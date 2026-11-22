"""Exact identity only; sample_id is part of the key, attempt number is not."""
from civitas.contracts.base import content_hash
from .invocations import encode

PROMPT_VERSION = 'wp05-evidence-json-v1'
PARSER_VERSION = 'wp05-strict-v1'


def semantic_key(request):
    # Tape replay changes transport source, never observation, memory, sample or model parameters.
    body = {**request, 'configuration': {**request['configuration']}}
    body['configuration'].pop('source')
    return content_hash(body)


def make_request(observation, memory, config, *, policy_id, policy_version, task, sample_id,
                 response_schema, task_context, mechanism_versions, institution_versions,
                 prompt_version=PROMPT_VERSION, parser_version=PARSER_VERSION):
    memory.validate_for(observation)
    if not sample_id:
        raise ValueError('Independent sample identity is required')
    body = dict(observation=observation.model_dump(mode='json'), memory=memory.model_dump(mode='json'),
        configuration=config.model_dump(mode='json'), policy_id=policy_id, policy_version=policy_version,
        task=task, sample_id=sample_id, response_schema=response_schema, task_context=task_context,
        prompt_version=prompt_version, parser_version=parser_version,
        mechanism_versions=mechanism_versions, institution_versions=institution_versions)
    prompt_body = {k: v for k, v in body.items() if k != 'configuration'}
    if 'memory_prompt_projection' in task_context:
        projection = task_context['memory_prompt_projection']
        if projection not in {'episode_references_v1','episode_index_v2'}:
            raise ValueError('Unknown memory prompt projection')
        # Full provenance remains in the cache identity; the model cites evidence IDs.
        episodes = [
            {k:v for k,v in episode.items() if k not in {'observation_id','content_hash'}}
            for episode in body['memory']['episodic']]
        if projection == 'episode_index_v2':
            # Pinned facts recur across ticks; index repeated labels, retaining every occurrence.
            references, timeline = {}, []
            for episode in episodes:
                key = (episode['evidence_id'],episode['source'])
                index = references.setdefault(key,len(references))
                timeline.append([index,episode['seen_at']])
            episodes = {'references':[{'evidence_id':key[0],'source':key[1]} for key in references],
                'timeline_columns':['reference_index','seen_at'],'timeline':timeline}
        prompt_body['memory'] = {**body['memory'],'episodic':episodes}
    if 'observation_prompt_projection' in task_context:
        if task_context['observation_prompt_projection'] != 'audience_sidecar_v1':
            raise ValueError('Unknown observation prompt projection')
        # Public resolutions repeat the same audience; share labels without changing visibility.
        audiences, references, market = {}, {}, []
        for fact in body['observation']['market_view']:
            audience = fact['value'].get('visible_to')
            if isinstance(audience,list) and all(isinstance(actor,str) for actor in audience):
                index = audiences.setdefault(tuple(audience),len(audiences))
                references[fact['fact_id']] = index
                fact = {**fact,'value':{k:v for k,v in fact['value'].items() if k!='visible_to'}}
            market.append(fact)
        prompt_body['observation'] = {**body['observation'],'market_view':market}
        prompt_body['observation_audiences'] = [list(audience) for audience in audiences]
        prompt_body['observation_audience_refs'] = references
    if 'strict_response_schema' in task_context:
        # The provider supplies the strict output schema; keep one wire copy under the input cap.
        prompt_body.pop('response_schema')
        prompt_body['task_context'] = {k:v for k,v in task_context.items()
            if k not in {'strict_response_schema','belief_schema'}}
    schema_label = 'output schema' if 'strict_response_schema' in task_context else 'response_schema'
    messages = [
        {'role': 'system', 'content': 'Return one JSON object matching the supplied '+schema_label+' and identities. '
         'Use only the visible observation and private memory. Cite visible evidence IDs. '
         'Do not return internal reasoning or chain of thought. Follow task_context. Version: ' + prompt_version},
        {'role': 'user', 'content': encode(prompt_body)}]
    body['wire'] = config.wire_request(messages)
    if 'strict_response_schema' in task_context:
        if config.provider!='openai':
            raise ValueError('Strict response transport is only configured for OpenAI')
        body['wire']['text']['format'] = {'type':'json_schema','name':'civitas_response','strict':True,
            'schema':task_context['strict_response_schema']}
    if len(encode(body['wire']).encode('utf-8')) + 1024 > config.max_input_tokens:
        raise ValueError('Request exceeds byte-conservative input reservation')
    return body
