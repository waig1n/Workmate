# Model configuration

[English](../en/models.md) | [简体中文](../zh-CN/models.md)

Choose the chat models, media inputs, and embedding models available to your team. The workspace and execution services share a model catalog, with credentials managed on the server.

## Profiles and credentials

`product.models.default` selects the default profile; `product.models.profiles` defines the catalog. Profiles declare fields such as `type`, `model`, `media_profile`, `context_size`, and `max_output_tokens`. `api_key_env` and `base_url_env` reference environment variable names, never literal secrets.

Merge this structural example into your configuration and replace the model ID:

```yaml
product:
  models:
    default: office
    media_profiles:
      text:
        model_inputs: [text]
        pdf_strategy: extract_text
    profiles:
      office:
        type: openai
        model: provider-model-id
        base_url_env: CHAT_MODEL_BASE_URL
        api_key_env: CHAT_MODEL_API_KEY
        media_profile: text
        supports_thinking: false
        context_size: 32768
        max_output_tokens: 4096
```

Pass referenced environment variables explicitly to the relevant services. Gateway and Runtime must use matching versions and model configuration; updating only one side can make the catalog disagree with execution capabilities.

## Media capabilities

`model_inputs` accepts `text`, `image`, `audio`, and `video`, and must include `text`. PDF handling uses `pdf_strategy: extract_text | native`; PDF is not a `model_inputs` value.

| Adapter | Text | Image | Audio | Video | Native PDF |
|---|---|---|---|---|---|
| `openai` | Yes | Yes | Yes | No | Yes |
| `dashscope` | Yes | Yes | Yes | Yes | No |

The table lists inputs the current model adapters can construct. Actual support also depends on your model and endpoint; configure only inputs supported by your provider. Unknown fields, missing media presets, and incompatible declarations fail configuration validation.

Runtime uses AgentScope's built-in ModelCard when a matching card exists; custom model IDs use a product-generated card. Test the relevant media against the actual model after changing capability declarations.

## Embeddings and long-term memory

ReMe runs inside Runtime and needs working model and embedding credentials. Knowledge uses a separate embedding profile and `KNOWLEDGE_EMBEDDING_MODEL_*` connection. Do not assume it shares ReMe's model or vector dimensions.

Check existing vector collection compatibility before changing embedding models or dimensions. Do not write vectors with different dimensions into the same collection. Conversation isolation is distinct from the user-and-Agent long-term memory namespace.

## Usage and costs

Usage reporting shows token consumption and estimated costs; unavailable usage remains unknown. Configured prices support cost analysis, while provider invoices determine actual charges. Billing settlement and hard budget enforcement are not currently provided.
