"""
.. module: lemur.plugins.lemur_acme.plugin
    :platform: Unix
    :synopsis: This module is responsible for communicating with an ACME CA.
    :copyright: (c) 2015 by Netflix Inc., see AUTHORS for more
    :license: Apache, see LICENSE for more details.

    Snippets from https://raw.githubusercontent.com/alex/letsencrypt-aws/master/letsencrypt-aws.py

.. moduleauthor:: Kevin Glisson <kglisson@netflix.com>
.. moduleauthor:: Mikhail Khodorovskiy <mikhail.khodorovskiy@jivesoftware.com>
.. moduleauthor:: Curtis Castrapel <ccastrapel@netflix.com>
"""
import json

import OpenSSL.crypto
import josepy as jose
from acme import challenges, messages
from acme.client import Client
from acme.messages import Error as AcmeError
from acme.errors import PollError
from botocore.exceptions import ClientError
from flask import current_app

from lemur.authorizations import service as authorization_service
from lemur.common.utils import generate_private_key
from lemur.dns_providers import service as dns_provider_service
from lemur.exceptions import InvalidAuthority, InvalidConfiguration, UnknownProvider
from lemur.plugins import lemur_acme as acme
from lemur.plugins.bases import IssuerPlugin
from lemur.plugins.lemur_acme import cloudflare, dyn, route53


def find_dns_challenge(authz):
    for combo in authz.body.resolved_combinations:
        if (
            len(combo) == 1 and
            isinstance(combo[0].chall, challenges.DNS01)
        ):
            yield combo[0]


class AuthorizationRecord(object):
    def __init__(self, host, authz, dns_challenge, change_id):
        self.host = host
        self.authz = authz
        self.dns_challenge = dns_challenge
        self.change_id = change_id


def start_dns_challenge(acme_client, account_number, host, dns_provider):
    current_app.logger.debug("Starting DNS challenge for {0}".format(host))
    authz = acme_client.request_domain_challenges(host)

    [dns_challenge] = find_dns_challenge(authz)

    change_id = dns_provider.create_txt_record(
        dns_challenge.validation_domain_name(host),
        dns_challenge.validation(acme_client.key),
        account_number
    )

    return AuthorizationRecord(
        host,
        authz,
        dns_challenge,
        change_id,
    )


def complete_dns_challenge(acme_client, account_number, authz_record, dns_provider):
    current_app.logger.debug("Finalizing DNS challenge for {0}".format(authz_record.host))
    dns_provider.wait_for_dns_change(authz_record.change_id, account_number=account_number)

    response = authz_record.dns_challenge.response(acme_client.key)

    verified = response.simple_verify(
        authz_record.dns_challenge.chall,
        authz_record.host,
        acme_client.key.public_key()
    )

    if not verified:
        raise ValueError("Failed verification")

    acme_client.answer_challenge(authz_record.dns_challenge, response)


def request_certificate(acme_client, authorizations, csr):
    cert_response, _ = acme_client.poll_and_request_issuance(
        jose.util.ComparableX509(
            OpenSSL.crypto.load_certificate_request(
                OpenSSL.crypto.FILETYPE_PEM,
                csr
            )
        ),
        authzrs=[authz_record.authz for authz_record in authorizations],
        mintime=60,
        max_attempts=10,
    )

    pem_certificate = OpenSSL.crypto.dump_certificate(
        OpenSSL.crypto.FILETYPE_PEM, cert_response.body
    ).decode('utf-8')

    full_chain = []
    for cert in acme_client.fetch_chain(cert_response):
        chain = OpenSSL.crypto.dump_certificate(OpenSSL.crypto.FILETYPE_PEM, cert)
        full_chain.append(chain.decode("utf-8"))
    pem_certificate_chain = "\n".join(full_chain)

    current_app.logger.debug("{0} {1}".format(type(pem_certificate), type(pem_certificate_chain)))
    return pem_certificate, pem_certificate_chain


def setup_acme_client(authority):
    if not authority.options:
        raise InvalidAuthority("Invalid authority. Options not set")
    options = {}

    for option in json.loads(authority.options):
        options[option["name"]] = option.get("value")
    email = options.get('email', current_app.config.get('ACME_EMAIL'))
    tel = options.get('telephone', current_app.config.get('ACME_TEL'))
    directory_url = options.get('acme_url', current_app.config.get('ACME_DIRECTORY_URL'))

    key = jose.JWKRSA(key=generate_private_key('RSA2048'))

    current_app.logger.debug("Connecting with directory at {0}".format(directory_url))
    client = Client(directory_url, key)

    registration = client.register(
        messages.NewRegistration.from_data(email=email)
    )

    current_app.logger.debug("Connected: {0}".format(registration.uri))

    client.agree_to_tos(registration)
    return client, registration


def get_domains(options):
    """
    Fetches all domains currently requested
    :param options:
    :return:
    """
    current_app.logger.debug("Fetching domains")

    domains = [options['common_name']]
    if options.get('extensions'):
        for name in options['extensions']['sub_alt_names']['names']:
            domains.append(name)

    current_app.logger.debug("Got these domains: {0}".format(domains))
    return domains


def get_authorizations(acme_client, account_number, domains, dns_provider):
    authorizations = []
    for domain in domains:
        authz_record = start_dns_challenge(acme_client, account_number, domain, dns_provider)
        authorizations.append(authz_record)
    return authorizations


def finalize_authorizations(acme_client, account_number, dns_provider, authorizations):
    try:
        for authz_record in authorizations:
            complete_dns_challenge(acme_client, account_number, authz_record, dns_provider)
    finally:
        for authz_record in authorizations:
            dns_challenge = authz_record.dns_challenge
            dns_provider.delete_txt_record(
                authz_record.change_id,
                account_number,
                dns_challenge.validation_domain_name(authz_record.host),
                dns_challenge.validation(acme_client.key)
            )

    return authorizations


class ACMEIssuerPlugin(IssuerPlugin):
    title = 'Acme'
    slug = 'acme-issuer'
    description = 'Enables the creation of certificates via ACME CAs (including Let\'s Encrypt)'
    version = acme.VERSION

    author = 'Netflix'
    author_url = 'https://github.com/netflix/lemur.git'

    options = [
        {
            'name': 'acme_url',
            'type': 'str',
            'required': True,
            'validation': '/^http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\(\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+$/',
            'helpMessage': 'Must be a valid web url starting with http[s]://',
        },
        {
            'name': 'telephone',
            'type': 'str',
            'default': '',
            'helpMessage': 'Telephone to use'
        },
        {
            'name': 'email',
            'type': 'str',
            'default': '',
            'validation': '/^?([-a-zA-Z0-9.`?{}]+@\w+\.\w+)$/',
            'helpMessage': 'Email to use'
        },
        {
            'name': 'certificate',
            'type': 'textarea',
            'default': '',
            'validation': '/^-----BEGIN CERTIFICATE-----/',
            'helpMessage': 'Certificate to use'
        },
    ]

    def __init__(self, *args, **kwargs):
        super(ACMEIssuerPlugin, self).__init__(*args, **kwargs)

    def get_dns_provider(self, type):
        provider_types = {
            'cloudflare': cloudflare,
            'dyn': dyn,
            'route53': route53,
        }
        provider = provider_types.get(type)
        if not provider:
            raise UnknownProvider("No such DNS provider: {}".format(type))
        return provider

    def get_ordered_certificate(self, pending_cert):
        acme_client, registration = setup_acme_client(pending_cert.authority)
        order_info = authorization_service.get(pending_cert.external_id)
        dns_provider = dns_provider_service.get(pending_cert.dns_provider_id)
        dns_provider_type = self.get_dns_provider(dns_provider.provider_type)
        try:
            authorizations = get_authorizations(
                acme_client, order_info.account_number, order_info.domains, dns_provider_type)
        except ClientError:
            current_app.logger.error("Unable to resolve pending cert: {}".format(pending_cert.name), exc_info=True)
            return False

        authorizations = finalize_authorizations(
            acme_client, order_info.account_number, dns_provider_type, authorizations)
        pem_certificate, pem_certificate_chain = request_certificate(acme_client, authorizations, pending_cert.csr)
        cert = {
            'body': "\n".join(str(pem_certificate).splitlines()),
            'chain': "\n".join(str(pem_certificate_chain).splitlines()),
            'external_id': str(pending_cert.external_id)
        }
        return cert

    def get_ordered_certificates(self, pending_certs):
        pending = []
        certs = []
        for pending_cert in pending_certs:
            try:
                acme_client, registration = setup_acme_client(pending_cert.authority)
                order_info = authorization_service.get(pending_cert.external_id)
                dns_provider = dns_provider_service.get(pending_cert.dns_provider_id)
                dns_provider_type = self.get_dns_provider(dns_provider.provider_type)

                authorizations = get_authorizations(
                    acme_client, order_info.account_number, order_info.domains, dns_provider_type)
                pending.append({
                    "acme_client": acme_client,
                    "account_number": order_info.account_number,
                    "dns_provider_type": dns_provider_type,
                    "authorizations": authorizations,
                    "pending_cert": pending_cert,
                })
            except (ClientError, ValueError, Exception):
                current_app.logger.error("Unable to resolve pending cert: {}".format(pending_cert), exc_info=True)
                certs.append({
                    "cert": False,
                    "pending_cert": pending_cert,
                })

        for entry in pending:
            try:
                entry["authorizations"] = finalize_authorizations(
                    entry["acme_client"],
                    entry["account_number"],
                    entry["dns_provider_type"],
                    entry["authorizations"]
                )
                pem_certificate, pem_certificate_chain = request_certificate(
                    entry["acme_client"],
                    entry["authorizations"],
                    entry["pending_cert"].csr
                )

                cert = {
                    'body': "\n".join(str(pem_certificate).splitlines()),
                    'chain': "\n".join(str(pem_certificate_chain).splitlines()),
                    'external_id': str(entry["pending_cert"].external_id)
                }
                certs.append({
                    "cert": cert,
                    "pending_cert": entry["pending_cert"],
                })
            except (PollError, AcmeError, Exception):
                current_app.logger.error("Unable to resolve pending cert: {}".format(pending_cert), exc_info=True)
                certs.append({
                    "cert": False,
                    "pending_cert": entry["pending_cert"],
                })
        return certs

    def create_certificate(self, csr, issuer_options):
        """
        Creates an ACME certificate.

        :param csr:
        :param issuer_options:
        :return: :raise Exception:
        """
        authority = issuer_options.get('authority')
        create_immediately = issuer_options.get('create_immediately', False)
        acme_client, registration = setup_acme_client(authority)
        dns_provider = issuer_options.get('dns_provider')
        if not dns_provider:
            raise InvalidConfiguration("DNS Provider setting is required for ACME certificates.")
        credentials = json.loads(dns_provider.credentials)

        current_app.logger.debug("Using DNS provider: {0}".format(dns_provider.provider_type))
        dns_provider_type = __import__(dns_provider.provider_type, globals(), locals(), [], 1)
        account_number = credentials.get("account_id")
        if dns_provider.provider_type == 'route53' and not account_number:
            error = "Route53 DNS Provider {} does not have an account number configured.".format(dns_provider.name)
            current_app.logger.error(error)
            raise InvalidConfiguration(error)
        domains = get_domains(issuer_options)
        if not create_immediately:
            # Create pending authorizations that we'll need to do the creation
            authz_domains = []
            for d in domains:
                if type(d) == str:
                    authz_domains.append(d)
                else:
                    authz_domains.append(d.value)

            dns_authorization = authorization_service.create(account_number, authz_domains, dns_provider.provider_type)
            # Return id of the DNS Authorization
            return None, None, dns_authorization.id

        authorizations = get_authorizations(acme_client, account_number, domains, dns_provider_type)
        finalize_authorizations(acme_client, account_number, dns_provider_type, authorizations)
        pem_certificate, pem_certificate_chain = request_certificate(acme_client, authorizations, csr)
        # TODO add external ID (if possible)
        return pem_certificate, pem_certificate_chain, None

    @staticmethod
    def create_authority(options):
        """
        Creates an authority, this authority is then used by Lemur to allow a user
        to specify which Certificate Authority they want to sign their certificate.

        :param options:
        :return:
        """
        role = {'username': '', 'password': '', 'name': 'acme'}
        plugin_options = options.get('plugin', {}).get('plugin_options')
        if not plugin_options:
            error = "Invalid options for lemur_acme plugin: {}".format(options)
            current_app.logger.error(error)
            raise InvalidConfiguration(error)
        # Define static acme_root based off configuration variable by default. However, if user has passed a
        # certificate, use this certificate as the root.
        acme_root = current_app.config.get('ACME_ROOT')
        for option in plugin_options:
            if option.get('name') == 'certificate':
                acme_root = option.get('value')
        return acme_root, "", [role]
