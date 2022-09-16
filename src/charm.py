#!/usr/bin/env python3
#
# Copyright 2021 Canonical Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
#
# Learn more at: https://juju.is/docs/sdk

"""Charm the service.

Refer to the following post for a quick-start guide that will help you
develop a new k8s charm using the Operator Framework:

    https://discourse.charmhub.io/t/4208
"""

import logging
from typing import Callable
from typing import List

import ops.charm
import ops.pebble
from ops.main import main
from ops.framework import StoredState
from ops import model

from utils import manager
import ops_sunbeam.charm as sunbeam_charm
import ops_sunbeam.core as sunbeam_core
import ops_sunbeam.config_contexts as sunbeam_contexts
import ops_sunbeam.relation_handlers as sunbeam_rhandlers

import charms.sunbeam_keystone_operator.v0.identity_service as sunbeam_id_svc
import charms.sunbeam_keystone_operator.v0.cloud_credentials as sunbeam_cc_svc

logger = logging.getLogger(__name__)

KEYSTONE_CONTAINER = "keystone"


KEYSTONE_CONF = '/etc/keystone/keystone.conf'
LOGGING_CONF = '/etc/keystone/logging.conf'


class KeystoneLoggingAdapter(sunbeam_contexts.ConfigContext):

    def context(self):
        config = self.charm.model.config
        ctxt = {}
        if config['debug']:
            ctxt['root_level'] = 'DEBUG'
        log_level = config['log-level']
        if log_level in ['DEBUG', 'INFO', 'WARNING', 'ERROR']:
            ctxt['log_level'] = log_level
        else:
            logger.error('log-level must be one of the following values '
                         f'(DEBUG, INFO, WARNING, ERROR) not "{log_level}"')
            ctxt['log_level'] = None
        ctxt['log_file'] = '/var/log/keystone/keystone.log'
        return ctxt


class KeystoneConfigAdapter(sunbeam_contexts.ConfigContext):

    def context(self):
        config = self.charm.model.config
        return {
            'api_version': 3,
            'admin_role': self.charm.admin_role,
            'assignment_backend': 'sql',
            'service_tenant_id': self.charm.service_project_id,
            'admin_domain_name': self.charm.admin_domain_name,
            'admin_domain_id': self.charm.admin_domain_id,
            'auth_methods': 'external,password,token,oauth1,mapped',
            'default_domain_id': self.charm.default_domain_id,
            'public_port': self.charm.service_port,
            'debug': config['debug'],
            'token_expiration': config['token-expiration'],
            'catalog_cache_expiration': config['catalog-cache-expiration'],
            'dogpile_cache_expiration': config['dogpile-cache-expiration'],
            'identity_backend': 'sql',
            'token_provider': 'fernet',
            'fernet_max_active_keys': config['fernet-max-active-keys'],
            'public_endpoint': self.charm.public_endpoint,
            'admin_endpoint': self.charm.admin_endpoint,
            'domain_config_dir': '/etc/keystone/domains',
            'log_config': '/etc/keystone/logging.conf.j2',
            'paste_config_file': '/etc/keystone/keystone-paste.ini',
        }


class IdentityServiceProvidesHandler(sunbeam_rhandlers.RelationHandler):

    def __init__(
        self,
        charm: ops.charm.CharmBase,
        relation_name: str,
        callback_f: Callable,
    ):
        super().__init__(charm, relation_name, callback_f)

    def setup_event_handler(self):
        """Configure event handlers for an Identity service relation."""
        logger.debug("Setting up Identity Service event handler")
        id_svc = sunbeam_id_svc.IdentityServiceProvides(
            self.charm,
            self.relation_name,
        )
        self.framework.observe(
            id_svc.on.ready_identity_service_clients,
            self._on_identity_service_ready)
        return id_svc

    def _on_identity_service_ready(self, event) -> None:
        """Handles AMQP change events."""
        # Ready is only emitted when the interface considers
        # that the relation is complete (indicated by a password)
        self.callback_f(event)

    @property
    def ready(self) -> bool:
        return True


class CloudCredentialsProvidesHandler(sunbeam_rhandlers.RelationHandler):

    def __init__(
        self,
        charm: ops.charm.CharmBase,
        relation_name: str,
        callback_f: Callable,
    ):
        super().__init__(charm, relation_name, callback_f)

    def setup_event_handler(self):
        """Configure event handlers for a Cloud Credentials relation."""
        logger.debug("Setting up Cloud Credentials event handler")
        id_svc = sunbeam_cc_svc.CloudCredentialsProvides(
            self.charm,
            self.relation_name,
        )
        self.framework.observe(
            id_svc.on.ready_cloud_credentials_clients,
            self._on_cloud_credentials_ready)
        return id_svc

    def _on_cloud_credentials_ready(self, event) -> None:
        """Handles cloud credentials change events."""
        # Ready is only emitted when the interface considers
        # that the relation is complete (indicated by a username)
        self.callback_f(event)

    @property
    def ready(self) -> bool:
        return True


class KeystoneOperatorCharm(sunbeam_charm.OSBaseOperatorAPICharm):
    """Charm the service."""

    _state = StoredState()
    _authed = False
    service_name = "keystone"
    wsgi_admin_script = '/usr/bin/keystone-wsgi-admin'
    wsgi_public_script = '/usr/bin/keystone-wsgi-public'
    service_port = 5000

    def __init__(self, framework):
        super().__init__(framework)
        self.keystone_manager = manager.KeystoneManager(
            self,
            KEYSTONE_CONTAINER)
        self._state.set_default(admin_domain_name='admin_domain')
        self._state.set_default(admin_domain_id=None)
        self._state.set_default(default_domain_id=None)
        self._state.set_default(service_project_id=None)

    def get_relation_handlers(self, handlers=None) -> List[
            sunbeam_rhandlers.RelationHandler]:
        """Relation handlers for the service."""
        handlers = handlers or []
        if self.can_add_handler('identity-service', handlers):
            self.id_svc = IdentityServiceProvidesHandler(
                self,
                'identity-service',
                self.register_service,
            )
            handlers.append(self.id_svc)

        if self.can_add_handler('identity-credentials', handlers):
            self.cc_svc = CloudCredentialsProvidesHandler(
                self,
                'identity-credentials',
                self.add_credentials,
            )
            handlers.append(self.cc_svc)

        return super().get_relation_handlers(handlers)

    @property
    def config_contexts(self) -> List[sunbeam_contexts.ConfigContext]:
        """Configuration adapters for the operator."""
        return [
            KeystoneConfigAdapter(self, 'ks_config'),
            KeystoneLoggingAdapter(self, 'ks_logging'),
            sunbeam_contexts.CharmConfigContext(self, 'options')]

    @property
    def container_configs(self):
        _cconfigs = super().container_configs
        _cconfigs.extend([
            sunbeam_core.ContainerConfigFile(
                LOGGING_CONF,
                'keystone',
                'keystone')])
        return _cconfigs

    def register_service(self, event):
        if not self._state.bootstrapped:
            event.defer()
            return
        if not self.unit.is_leader():
            return
        relation = self.model.get_relation(
            event.relation_name,
            event.relation_id)
        binding = self.framework.model.get_binding(relation)
        ingress_address = str(binding.network.ingress_address)
        service_domain = self.keystone_manager.create_domain(
            name='service_domain',
            may_exist=True)
        service_project = self.keystone_manager.get_project(
            name=self.service_project,
            domain=service_domain)
        admin_domain = self.keystone_manager.get_domain(
            name='admin_domain')
        admin_project = self.keystone_manager.get_project(
            name='admin',
            domain=admin_domain)
        admin_user = self.keystone_manager.get_user(
            name=self.model.config['admin-user'],
            project=admin_project,
            domain=admin_domain)
        admin_role = self.keystone_manager.create_role(
            name=self.admin_role,
            may_exist=True)
        for ep_data in event.service_endpoints:
            service_username = 'svc_{}'.format(
                event.client_app_name.replace('-', '_'))
            service_password = 'password123'
            service_user = self.keystone_manager.create_user(
                name=service_username,
                password=service_password,
                domain=service_domain.id,
                may_exist=True)
            self.keystone_manager.grant_role(
                role=admin_role,
                user=service_user,
                project=service_project,
                may_exist=True)
            service = self.keystone_manager.create_service(
                name=ep_data['service_name'],
                service_type=ep_data['type'],
                description=ep_data['description'],
                may_exist=True)
            for interface in ['admin', 'internal', 'public']:
                self.keystone_manager.create_endpoint(
                    service=service,
                    interface=interface,
                    url=ep_data[f'{interface}_url'],
                    region=event.region,
                    may_exist=True)
            self.id_svc.interface.set_identity_service_credentials(
                event.relation_name,
                event.relation_id,
                'v3',
                ingress_address,
                self.default_public_ingress_port,
                'http',
                ingress_address,
                self.default_public_ingress_port,
                'http',
                ingress_address,
                self.default_public_ingress_port,
                'http',
                admin_domain,
                admin_project,
                admin_user,
                service_domain,
                service_password,
                service_project,
                service_user,
                self.internal_endpoint,
                self.admin_endpoint,
                self.public_endpoint)

    def add_credentials(self, event):
        """

        :param event:
        :return:
        """
        if not self.unit.is_leader():
            logger.debug('Current unit is not the leader unit, deferring '
                         'credential creation to leader unit.')
            return

        if not self.bootstrapped():
            logger.debug('Keystone is not bootstrapped, deferring credential '
                         'creation until after bootstrap.')
            event.defer()
            return

        relation = self.model.get_relation(
            event.relation_name,
            event.relation_id)
        binding = self.framework.model.get_binding(relation)
        ingress_address = str(binding.network.ingress_address)
        service_domain = self.keystone_manager.create_domain(
            name='service_domain',
            may_exist=True)
        service_project = self.keystone_manager.get_project(
            name=self.service_project,
            domain=service_domain)
        service_user = self.keystone_manager.create_user(
            name=event.username,
            password='password123',
            domain=service_domain.id,
            may_exist=True)
        admin_role = self.keystone_manager.create_role(
            name=self.admin_role,
            may_exist=True)
        # TODO(wolsen) let's not always grant admin role!
        self.keystone_manager.grant_role(
            role=admin_role,
            user=service_user,
            project=service_project,
            may_exist=True)

        self.cc_svc.interface.set_cloud_credentials(
            relation_name=event.relation_name,
            relation_id=event.relation_id,
            api_version='3',
            auth_host=ingress_address,
            auth_port=self.default_public_ingress_port,
            auth_protocol='http',
            internal_host=ingress_address,  # XXX(wolsen) internal address?
            internal_port=self.default_public_ingress_port,
            internal_protocol='http',
            username=service_user.name,
            password='password123',
            project_name=service_project.name,
            project_id=service_project.id,
            user_domain_name=service_domain.name,
            user_domain_id=service_domain.id,
            project_domain_name=service_domain.name,
            project_domain_id=service_domain.id,
            region=self.model.config['region'],  # XXX(wolsen) region matters?
        )

    @property
    def default_public_ingress_port(self):
        return 5000

    @property
    def default_domain_id(self):
        return self._state.default_domain_id

    @property
    def admin_domain_name(self):
        return self._state.admin_domain_name

    @property
    def admin_domain_id(self):
        return self._state.admin_domain_id

    @property
    def admin_password(self):
        # TODO(wolsen) password stuff
        return 'abc123'

    @property
    def admin_user(self):
        return self.model.config['admin-user']

    @property
    def admin_role(self):
        return self.model.config['admin-role']

    @property
    def charm_user(self):
        """The admin user specific to the charm.

        This is a special admin user reserved for the charm to interact with
        keystone.
        """
        return '_charm-keystone-admin'

    @property
    def charm_password(self):
        # TODO
        return 'abc123'

    @property
    def service_project(self):
        return self.model.config['service-tenant']

    @property
    def service_project_id(self):
        return self._state.service_project_id

    @property
    def admin_endpoint(self):
        admin_hostname = self.model.config.get('os-admin-hostname')
        if not admin_hostname:
            admin_hostname = self.model.get_binding(
                "identity-service"
            ).network.ingress_address
        return f'http://{admin_hostname}:{self.service_port}'

    @property
    def internal_endpoint(self):
        if self.ingress_internal and self.ingress_internal.url:
            return self.ingress_internal.url

        internal_hostname = self.model.config.get('os-internal-hostname')
        if not internal_hostname:
            internal_hostname = self.model.get_binding(
                "identity-service"
            ).network.ingress_address
        return f'http://{internal_hostname}:{self.service_port}'

    @property
    def public_endpoint(self):
        if self.ingress_public and self.ingress_public.url:
            return self.ingress_public.url

        address = self.public_ingress_address
        if not address:
            address = self.model.get_binding(
                'identity-service'
            ).network.ingress_address
        return f'http://{address}:{self.service_port}'

    def _do_bootstrap(self):
        """
        Starts the appropriate services in the order they are needed.
        If the service has not yet been bootstrapped, then this will
         1. Create the database
         2. Bootstrap the keystone users service
         3. Setup the fernet tokens
        """
        super()._do_bootstrap()
        if self.unit.is_leader():
            try:
                self.keystone_manager.setup_keystone()
            except ops.pebble.ExecError:
                logger.exception('Failed to bootstrap')
                self._state.bootstrapped = False
                return
            self.keystone_manager.setup_initial_projects_and_users()
        self.unit.status = model.MaintenanceStatus('Starting Keystone')

    def _ingress_changed(self, event: ops.framework.EventBase) -> None:
        """Ingress changed callback.

        Invoked when the data on the ingress relation has changed. This will
        update the keystone endpoints, and then call the configure_charm.
        """
        logger.debug('Received an ingress_changed event')
        if self.bootstrapped():
            self.keystone_manager.update_service_catalog_for_keystone()
        self.configure_charm(event)


class KeystoneXenaOperatorCharm(KeystoneOperatorCharm):

    openstack_release = 'xena'


if __name__ == "__main__":
    # Note: use_juju_for_storage=True required per
    # https://github.com/canonical/operator/issues/506
    main(KeystoneXenaOperatorCharm, use_juju_for_storage=True)
