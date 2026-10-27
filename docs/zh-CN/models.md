# 模型配置

[English](../en/models.md) | [简体中文](../zh-CN/models.md)

配置团队可用的聊天模型、媒体输入和向量模型。模型目录统一提供给工作台与执行服务，凭据由服务端管理。

## 模型配置与凭据

`product.models.default` 选择默认模型配置（Profile），`product.models.profiles` 定义模型目录。每个配置填写 `type`、`model`、`media_profile`、`context_size`、`max_output_tokens` 等声明；`api_key_env` 与 `base_url_env` 引用环境变量名，不填写密钥实值。

将以下示例合并到现有配置中，并替换模型 ID：

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

对应环境变量必须显式传入需要的服务。相同版本的 Gateway 与 Runtime 使用相同模型配置；只改一侧会造成目录与执行能力不一致。

## 媒体能力

`model_inputs` 可包含 `text`、`image`、`audio`、`video`，必须包含 `text`。PDF 使用独立的 `pdf_strategy: extract_text | native`，不作为 `model_inputs` 值。

| 模型适配器 | 文本 | 图片 | 音频 | 视频 | 原生 PDF |
|---|---|---|---|---|---|
| `openai` | 支持 | 支持 | 支持 | 不支持 | 支持 |
| `dashscope` | 支持 | 支持 | 支持 | 支持 | 不支持 |

此表列出当前模型适配器可构造的输入类型。实际可用能力还取决于模型与服务端点，请按供应商支持的输入配置。未知字段、缺失的媒体配置和不兼容声明会导致配置校验失败。

如果 AgentScope 已提供同名模型的内置能力定义（ModelCard），Runtime 优先使用该定义；自定义模型则使用平台配置生成的定义。调整后请用实际图片、音频或其他所需输入测试模型。

## 向量模型与长期记忆

ReMe 是 Runtime 进程内的长期记忆组件，其聊天模型和向量模型配置需要可用凭据。知识库使用独立的向量模型配置 和 `KNOWLEDGE_EMBEDDING_MODEL_*` 连接，不能默认与 ReMe 共用模型或向量维度。

更换向量模型或维度前，请检查已有向量集合的兼容性；不同维度的向量需使用兼容的集合。同一用户与智能体的长期记忆可以跨会话使用。

## 用量与成本

用量页面展示 token 消耗与估算成本；未获取到的用量按未知处理。配置价格用于成本分析，实际费用以供应商账单为准。当前不提供收费结算或预算额度的强制控制。
