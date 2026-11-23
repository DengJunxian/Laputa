"""Fixed, exact-request response tapes for offline supplier experiments."""
from typing import Any, Literal

from pydantic import Field, model_validator

from civitas.contracts.base import Record, require_unique
from civitas.contracts.observations import Observation
from .cache import semantic_key
from .invocations import safe_value
from .providers import ProviderConfig, ProviderResponse


MAX_ARCHIVE_SLOTS = 248
SUPPLIER_TASKS = ('cd_supplier', 'cd_reflection')
CHOICE_TASKS = ('cd_withdrawal', 'cd_labor', 'cd_cooperation_choice', 'cd_disclosure_choice', 'cd_credit',
                'joint_policy_choice')


class SupplierArchiveRow(Record):
    request: dict[str, Any]
    response: ProviderResponse


class SupplierResponseArchive(Record):
    schema_version: Literal[1] = 1
    records: list[SupplierArchiveRow] = Field(min_length=1, max_length=MAX_ARCHIVE_SLOTS)

    @property
    def provider_config(self):
        return ProviderConfig.model_validate(self.records[0].request.get('configuration')).model_copy(
            update={'source': 'archived_response'})

    @model_validator(mode='after')
    def verify_offline_rows(self):
        if safe_value(self) != self.model_dump(mode='json'):
            raise ValueError('Credential-bearing response archives are not permitted')
        expected = self.provider_config
        slots = []
        for row in self.records:
            request, response = row.request, row.response
            provider = ProviderConfig.model_validate(request.get('configuration'))
            if (provider.source not in ('synthetic_test', 'archived_response') or provider.provider != 'synthetic'
                    or provider.price.input_nano or provider.price.output_nano or provider.endpoint
                    or provider.max_attempts != 1 or provider.model_copy(update={'source': 'archived_response'}) != expected
                    or response.origin != 'synthetic_test' or response.provider != provider.provider
                    or response.model not in {provider.model, provider.revision}):
                raise ValueError('Response archive must contain one consistent zero-price fixture provider')
            observation = Observation.model_validate(request.get('observation'))
            if request.get('task') not in SUPPLIER_TASKS + CHOICE_TASKS or request.get('sample_id') != '1':
                raise ValueError('Only registered structured economic decisions are supported')
            slots.append((observation.actor_id, observation.visible_until.tick, request['task']))
        require_unique(slots, 'archived supplier slots')
        return self

    @staticmethod
    def permits_supplier(settings, task, actor, tick):
        return (settings is not None and task in SUPPLIER_TASKS
                and actor in {f'household:{i:04d}' for i in settings.household_indices}
                and tick in settings.ticks
                and (task != 'cd_reflection' or (settings.reflect and tick != settings.ticks[0])))

    def validate_slots(self, settings):
        for row in self.records:
            request, observation = row.request, row.request['observation']
            if not self.permits_supplier(settings, request['task'], observation['actor_id'],
                                         observation['visible_until']['tick']):
                raise ValueError('Response archive contains an unregistered supplier or reflection slot')

    def validate_registration(self, config):
        slots = {(task, f'household:{seat:04d}', tick) for task, seat, tick in config.choice_slots}
        slots.update(config.joint_model_slots)
        for row in self.records:
            request, observation = row.request, row.request['observation']
            key = (request['task'], observation['actor_id'], observation['visible_until']['tick'])
            if key not in slots and not self.permits_supplier(config.supplier_cognition, *key):
                raise ValueError('Response archive contains an unregistered decision slot')

    def replay_rows(self):
        return [dict(request_id=semantic_key(row.request), number=1, state='success' if row.response.ok else 'failed',
                     request=row.request, response=row.response.model_dump(mode='json')) for row in self.records]

    def validate_choice_slot(self, task, actor, tick):
        if len(self.records) != 1:
            raise ValueError('Register exactly one choice response')
        request = self.records[0].request
        observation = request['observation']
        if (request['task'], observation['actor_id'], observation['visible_until']['tick']) != (
                task, actor, tick):
            raise ValueError('Response archive does not identify the registered choice slot')
