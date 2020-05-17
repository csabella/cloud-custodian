# Copyright 2015-2017 Capital One Services, LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from collections import deque
import logging

from botocore.exceptions import ClientError

from c7n import cache
from c7n.executor import ThreadPoolExecutor
from c7n.provider import clouds
from c7n.registry import PluginRegistry
from c7n.resources import load_resources
try:
    from c7n.resources.aws import AWS
    resources = AWS.resources
except ImportError:
    resources = PluginRegistry('resources')

from c7n.utils import dumps


def iter_filters(filters, block_end=False):
    queue = deque(filters)
    while queue:
        f = queue.popleft()
        if f is not None and f.type in ('or', 'and', 'not'):
            if block_end:
                queue.appendleft(None)
            for gf in f.filters:
                queue.appendleft(gf)
        yield f


class ResourceManager:
    """
    A Cloud Custodian resource
    """

    filter_registry = None
    action_registry = None
    executor_factory = ThreadPoolExecutor
    retry = None

    def __init__(self, ctx, data):
        self.ctx = ctx
        self.session_factory = ctx.session_factory
        self.config = ctx.options
        self.data = data
        self._cache = cache.factory(self.ctx.options)
        self.log = logging.getLogger('custodian.resources.%s' % (
            self.__class__.__name__.lower()))

        if self.filter_registry:
            self.filters = self.filter_registry.parse(
                self.data.get('filters', []), self)
        if self.action_registry:
            self.actions = self.action_registry.parse(
                self.data.get('actions', []), self)

    def format_json(self, resources, fh):
        return dumps(resources, fh, indent=2)

    def match_ids(self, ids):
        """return ids that match this resource type's id format."""
        return ids

    @classmethod
    def get_permissions(cls):
        return ()

    def get_resources(self, resource_ids):
        """Retrieve a set of resources by id."""
        return []

    def resources(self):
        raise NotImplementedError("")

    def get_resource_manager(self, resource_type, data=None):
        """get a resource manager or a given resource type.

        assumes the query is for the same underlying cloud provider.
        """
        if '.' in resource_type:
            provider_name, resource_type = resource_type.split('.', 1)
        else:
            provider_name = self.ctx.policy.provider_name

        # check and load
        load_resources(('%s.%s' % (provider_name, resource_type),))
        provider_resources = clouds[provider_name].resources
        klass = provider_resources.get(resource_type)
        if klass is None:
            raise ValueError(resource_type)

        # if we're already querying via config carry it forward
        if not data and self.source_type == 'config' and getattr(
                klass.get_model(), 'config_type', None):
            return klass(self.ctx, {'source': self.source_type})
        return klass(self.ctx, data or {})

    def filter_resources(self, resources, event=None):
        original = len(resources)
        if event and event.get('debug', False):
            self.log.info(
                "Filtering resources with %s", self.filters)
        for f in self.filters:
            if not resources:
                break
            rcount = len(resources)

            with self.ctx.tracer.subsegment("filter:%s" % f.type):
                resources = f.process(resources, event)

            if event and event.get('debug', False):
                self.log.debug(
                    "applied filter %s %d->%d", f, rcount, len(resources))
        self.log.debug("Filtered from %d to %d %s" % (
            original, len(resources), self.__class__.__name__.lower()))
        return resources

    def get_model(self):
        """Returns the resource meta-model.
        """
        return self.query.resolve(self.resource_type)

    def iter_filters(self, block_end=False):
        return iter_filters(self.filters, block_end=block_end)

    def call_api(self, func, *args, ignore_not_found=True, not_found_codes=None, **kwargs):
        """Call a boto API with args and kwargs.

        Apply standard error handling to an API call, such as skipping Resoure Not Found
        exceptions.  This function will ignore the exceptions that don't need to raise
        an error.

        Args:
            func: API to call.
            ignore_not_found: Boolean set to False to always raise an exception.
            not_found_codes: Iterable of exceptions to ignore.
        """
        if not_found_codes is None:
            not_found_codes = self.resource_type.not_found_exceptions
        try:
            return func(*args, **kwargs)
        except ClientError as e:
            if not ignore_not_found or e.response['Error']['Code'] not in not_found_codes:
                raise

    def validate(self):
        """
        Validates resource definition, does NOT validate filters, actions, modes.

        Example use case: A resource type that requires an additional query

        :example:

        .. code-block:: yaml

            policies:
              - name: k8s-custom-resource
                resource: k8s.custom-namespaced-resource
                query:
                  - version: v1
                    group stable.example.com
                    plural: crontabs
        """
        pass
