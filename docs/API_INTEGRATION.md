# VAF API Integration Guide

VAF now supports multiple AI providers through API integration, allowing you to use commercial AI services alongside or instead of local models.

## Supported Providers

- **Local** - llama-server (default, runs locally)
- **OpenAI** - GPT-4, GPT-4o, GPT-3.5-turbo
- **Anthropic** - Claude 3.5 Sonnet, Claude 3 Opus/Sonnet/Haiku
- **DeepSeek** - DeepSeek Chat, DeepSeek Coder
- **Google AI Studio** - Gemini 1.5 Pro/Flash, Gemini 1.0 Pro
- **OpenRouter** - Multi-provider access (Claude, GPT-4, Llama, etc.)

## Configuration

### 1. Set AI Provider

Run VAF and open settings:

```bash
vaf run
# Press 's' for settings
# Select "🌐 AI Provider: LOCAL"
```

Choose your provider and enter API key when prompted.

### 2. API Key Management

**Best Practices Implemented:**

- API keys are **Base64 encoded** for basic obfuscation (not encryption)
- Keys are stored in `~/.vaf/config.json`
- Keys are **masked** when displayed (e.g., `sk-proj1...ab2c`)
- **Connection testing** before saving to verify validity

**Manual Configuration:**

Edit `~/.vaf/config.json`:

```json
{
  "provider": "anthropic",
  "api_key_anthropic": "BASE64_ENCODED_KEY_HERE"
}
```

**Using Python to encode keys:**

```python
import base64
from vaf.core.config import Config

# Set API key (automatically encodes)
Config.set_api_key("anthropic", "sk-ant-your-actual-key-here")

# Get API key (automatically decodes)
key = Config.get_api_key("anthropic")
```

### 3. Model Selection

Each provider has default models, but you can customize:

```json
{
  "provider": "openai",
  "api_model_openai": "gpt-4o",
  "api_model_anthropic": "claude-3-5-sonnet-20241022",
  "api_model_deepseek": "deepseek-chat",
  "api_model_google": "gemini-1.5-pro",
  "api_model_openrouter": "anthropic/claude-3.5-sonnet"
}
```

## Sub-Agent Provider Configuration

**Sub-agents can use a different provider than the main agent!**

Example use cases:
- **Main:** Claude API (high quality) | **Sub-Agents:** Local (free, fast)
- **Main:** Local (privacy) | **Sub-Agents:** GPT-4 (code generation)

### Configuration

1. Open Settings → "🔧 Sub-Agent Provider"
2. Choose provider for sub-agents:
   - **Inherit** - Use same provider as main agent (default)
   - **Local** - Always use local model for sub-agents
   - **API Provider** - Use specific API for sub-agents

**Config File:**

```json
{
  "provider": "anthropic",
  "subagent_provider": "local",
  "subagent_use_separate_provider": true
}
```

## Server Auto-Start

When using API providers, you may want to disable automatic llama-server startup:

```json
{
  "provider": "openai",
  "auto_start_local_server": false
}
```

This saves resources when not using local models.

## API Features

### Streaming

All API providers support **streaming responses** for real-time output.

### Tool Calling

- ✅ **OpenAI** - Full support
- ✅ **Anthropic** - Full support
- ✅ **OpenRouter** - Provider-dependent
- ⚠️ **DeepSeek** - Limited support
- ⚠️ **Google** - Limited support

### Context Windows

Provider context limits are respected:
- **GPT-4o**: ~128K tokens
- **Claude 3.5 Sonnet**: ~200K tokens
- **Gemini 1.5 Pro**: ~2M tokens
- **DeepSeek**: ~32K tokens

## Error Handling

### Common Issues

**1. Invalid API Key**

```
API Backend Error: API key not set for provider: anthropic
```

**Solution:** Configure API key in settings or manually in config.json

**2. Rate Limiting**

```
API request failed: 429 - Rate limit exceeded
```

**Solution:** Wait or upgrade API plan

**3. Network Timeout**

```
API request timed out for openai
```

**Solution:** Check internet connection, API may be overloaded

### Best Practices

1. **Test API keys** after entry (automatic in settings menu)
2. **Monitor token usage** - APIs charge per token
3. **Use local for development** - Switch to API for production
4. **Sub-agent optimization** - Use cheaper/faster model for sub-tasks

## Security Considerations

⚠️ **Important Security Notes:**

1. **Base64 is NOT encryption** - It's basic obfuscation only
2. **Config file is plain text** - Store `~/.vaf/` with restricted permissions
3. **For production**, consider:
   - System keyring integration (e.g., `keyring` Python package)
   - Environment variables: `export ANTHROPIC_API_KEY="..."`
   - Secret management services (AWS Secrets Manager, etc.)

**Recommended File Permissions:**

```bash
chmod 600 ~/.vaf/config.json
```

## API Provider Details

### OpenAI

**Models:**
- `gpt-4o` - Latest, multimodal (recommended)
- `gpt-4-turbo` - Fast, large context
- `gpt-3.5-turbo` - Cheaper, faster

**Get API Key:** https://platform.openai.com/api-keys

### Anthropic (Claude)

**Models:**
- `claude-3-5-sonnet-20241022` - Best balance (recommended)
- `claude-3-5-haiku-20241022` - Fast, cheaper
- `claude-3-opus-20240229` - Most capable

**Get API Key:** https://console.anthropic.com/

### DeepSeek

**Models:**
- `deepseek-chat` - General purpose
- `deepseek-coder` - Code-specialized

**Get API Key:** https://platform.deepseek.com/

### Google AI Studio

**Models:**
- `gemini-1.5-pro-latest` - Best quality, 2M context (recommended)
- `gemini-1.5-flash-latest` - Fast, 1M context
- `gemini-pro` - Legacy, 32K context

**Important:** Use `-latest` suffix for Gemini 1.5 models!

**Get API Key:** https://makersuite.google.com/app/apikey

**See also:** `docs/GOOGLE_GEMINI_MODELS.md` for detailed model info

### OpenRouter

**Multi-provider access** through single API key.

**Popular Models:**
- `anthropic/claude-3.5-sonnet`
- `openai/gpt-4o`
- `google/gemini-pro-1.5`
- `meta-llama/llama-3.1-405b-instruct`

**Get API Key:** https://openrouter.ai/keys

## Example Workflows

### 1. High-Quality Research with Budget Optimization

**Setup:**
- Main Agent: Claude 3.5 Sonnet (API) - Best reasoning
- Sub-Agents: Local Model - Free, handle simple tasks

**Use Case:** Research reports where main analysis needs high quality, but file processing can use local model.

### 2. Code Generation with Privacy

**Setup:**
- Main Agent: Local Model - Keep prompts private
- Sub-Agents: GPT-4 (API) - Generate code in isolated sub-agents

**Use Case:** Sensitive project with public code generation needs.

### 3. All-API Setup

**Setup:**
- Main Agent: OpenRouter (multi-model access)
- Sub-Agents: DeepSeek (cheap, fast)
- Local Server: Disabled (`auto_start_local_server: false`)

**Use Case:** Cloud/server deployment, no local GPU.

## Troubleshooting

### Test API Connection

```python
from vaf.core.api_backend import APIBackendManager

# Test specific provider
result = APIBackendManager.test_connection("anthropic")
print(f"Connection test: {'✓ Success' if result else '✗ Failed'}")
```

### Check Current Provider

```python
from vaf.core.config import Config

print(f"Provider: {Config.get('provider')}")
print(f"Sub-Agent Provider: {Config.get('subagent_provider')}")
```

### Reset to Local

```python
from vaf.core.config import Config

Config.set("provider", "local")
Config.set("auto_start_local_server", True)
```

## Performance Tips

1. **Use streaming** - Faster perceived response time
2. **Choose right model** - Balance cost vs. quality
3. **Local for iteration** - Develop with local, deploy with API
4. **Sub-agent optimization** - Use cheaper models for simple tasks
5. **Monitor token usage** - APIs charge per token

## Next Steps

- Configure your first API provider in Settings
- Test with a simple query
- Experiment with sub-agent provider separation
- Monitor costs and optimize model selection

For more help, see:
- Main Documentation: `docs/README.md`
- Configuration: `docs/CONFIGURATION.md`
- Sub-Agents: `docs/SUBAGENT_IPC.md`
