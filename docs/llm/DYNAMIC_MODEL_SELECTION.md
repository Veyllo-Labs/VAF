# Dynamic Model Selection

VAF now fetches available models **dynamically from API providers** instead of using static lists. This ensures you always have access to the latest models!

## How It Works

### 1. Automatic Model Discovery

When you configure an API provider, VAF automatically:
1. ✅ Tests your API key
2. ✅ Fetches all available models from the provider
3. ✅ Presents them in an interactive menu
4. ✅ Allows custom model IDs for new/beta models

### 2. Supported Providers

| Provider | Models Fetched | API Endpoint |
|----------|---------------|--------------|
| **OpenAI** | ✅ Dynamic | `GET /v1/models` |
| **Anthropic** | ✅ Dynamic (Web UI path) | `GET /v1/models` |
| **Google** | ✅ Dynamic | `GET /v1beta/models` |
| **OpenRouter** | ✅ Dynamic | `GET /v1/models` |
| **DeepSeek** | ✅ Dynamic | `GET /v1/models` |

## Usage

### During API Key Setup

```
Enter GOOGLE API key: [your key]
✓ API key verified!
| Loading Fetching available GOOGLE models...
| Success Found 12 models

Select GOOGLE model (12 available):
  > gemini-1.5-pro-latest
    gemini-1.5-flash-latest
    gemini-1.5-pro-002
    gemini-1.5-flash-002
    gemini-pro
    gemini-pro-vision
    ...
    Keep current
    Enter custom model ID
```

### Change Model Anytime

In Settings menu:
```
Settings Menu:
  🌐 AI Provider: GOOGLE
  🤖 API Model: gemini-1.5-pro-latest  ← New menu item!
  🔧 Sub-Agent Provider: LOCAL (inherited)
  ─────────────────
  ...
```

Select "🤖 API Model" to:
- View all available models (dynamically fetched)
- Switch to a different model
- Enter a custom model ID

## Features

### ✅ Real-Time Model Lists

Models are fetched **directly from the API** when you configure a provider:

```python
# Example: OpenAI
GET https://api.openai.com/v1/models
→ Returns: gpt-4o, gpt-4o-mini, gpt-4-turbo, ...

# Example: Google
GET https://generativelanguage.googleapis.com/v1beta/models?key=...
→ Returns: gemini-1.5-pro-latest, gemini-1.5-flash-latest, ...
```

### ✅ Custom Model Support

New model released? Just enter the ID:
```
Select Model:
  > Enter custom model ID

Enter custom model ID: gpt-4o-2025-01-15
✓ Model set to: gpt-4o-2025-01-15
```

### ✅ Current Model Indicator

Your current model is marked:
```
Select Model:
  ✓ gemini-1.5-pro-latest (current)
    gemini-1.5-flash-latest
    gemini-1.5-pro-002
    ...
```

### ✅ Fallback to Static Lists

If API fetch fails (network issue, rate limit), VAF falls back to a curated static list:

```python
# Fallback lists (automatically used if dynamic fetch fails)
"openai": ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", ...]
"google": ["gemini-1.5-pro-latest", "gemini-1.5-flash-latest", ...]
```

## Implementation Details

### Dynamic Fetching Functions

```python
# OpenAI
def _fetch_openai_models():
    response = requests.get(
        "https://api.openai.com/v1/models",
        headers={"Authorization": f"Bearer {api_key}"}
    )
    # Filter for chat models (gpt-4, gpt-3.5)
    return [model for model in data if "gpt" in model]

# Google
def _fetch_google_models():
    response = requests.get(
        f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
    )
    # Filter for models that support generateContent
    return [model for model in data if "generateContent" in supported_methods]

# OpenRouter
def _fetch_openrouter_models():
    response = requests.get(
        "https://openrouter.ai/api/v1/models",
        headers={"Authorization": f"Bearer {api_key}"}
    )
    return [model["id"] for model in data["data"]]
```

### Smart Filtering

Models are automatically filtered:
- **OpenAI**: Only chat models (gpt-4, gpt-3.5)
- **Google**: Only generative models with generateContent support
- **OpenRouter**: All available models (sorted)
- **DeepSeek**: All available models

### Caching Strategy

Models are fetched:
- ✅ When setting up a new provider
- ✅ When opening the model selection menu
- ❌ Not cached between sessions (always fresh)

## Examples

### Example 1: OpenAI with Latest Models

```bash
# Setup OpenAI provider
vaf run → Settings → AI Provider → OpenAI
Enter API key: sk-proj...

# Models fetched dynamically:
- gpt-4o (latest)
- gpt-4o-mini
- gpt-4-turbo-2024-04-09
- gpt-4-turbo-preview
- gpt-3.5-turbo
- gpt-3.5-turbo-16k
```

### Example 2: Google with All Gemini Variants

```bash
# Setup Google provider
vaf run → Settings → AI Provider → Google AI Studio
Enter API key: AIza...

# Models fetched from Google API:
- gemini-1.5-pro-latest
- gemini-1.5-pro-002
- gemini-1.5-pro-001
- gemini-1.5-flash-latest
- gemini-1.5-flash-002
- gemini-1.5-flash-001
- gemini-pro
- gemini-pro-vision
```

### Example 3: OpenRouter Multi-Provider

```bash
# Setup OpenRouter
vaf run → Settings → AI Provider → OpenRouter
Enter API key: sk-or-...

# 30+ models fetched:
- anthropic/claude-3.5-sonnet
- anthropic/claude-3-opus
- openai/gpt-4o
- openai/gpt-4-turbo
- google/gemini-pro-1.5
- meta-llama/llama-3.1-405b-instruct
- mistralai/mistral-large
- cohere/command-r-plus
... (sorted alphabetically)
```

## Benefits

### 🎯 Always Up-to-Date

No need to update VAF when providers release new models. Models are fetched directly from the API.

### 🚀 New Models Available Immediately

Released today? Available today:
```
# GPT-4.5 released? Just:
Settings → API Model → Enter custom model ID: gpt-4.5
```

### 🔍 Discover Models You Didn't Know Existed

OpenRouter has 100+ models. Dynamic fetching shows them all:
```
- nousresearch/nous-hermes-2-mixtral-8x7b
- teknium/openhermes-2.5-mistral-7b
- phind/phind-codellama-34b-v2
... and many more!
```

### 💡 Smart Filtering

Only relevant models shown:
- OpenAI: Chat models only (no embeddings/audio)
- Google: Text generation models only (no vision-only)
- No deprecated models

## Vision Model Fallback

Some providers (notably DeepSeek) do not support image input. VAF lets you configure a separate **Vision Model** that is used automatically whenever the primary model cannot process images.

### Configuration

**Settings → AI & Model → Vision-Modell**

| Setting | Description |
|---------|-------------|
| `vision_provider` | Provider to use for vision tasks (`openai`, `anthropic`, `google`, `openrouter`). Leave empty to show an error when images are attached. |
| `vision_model` | Specific model for vision. Leave empty to use the provider's default. |

### How it works

1. User sends a message with an image attached.
2. If the primary provider supports vision (Anthropic, Google, OpenAI with `gpt-4o`, etc.) → image is processed normally.
3. If the primary provider **does not** support vision (e.g. DeepSeek) and a `vision_provider` is configured:
   - VAF makes a short auxiliary call to the vision provider to analyse the image.
   - The description is injected into the message as `[Vision (provider/model): ...]`.
   - The primary model (DeepSeek etc.) then answers based on the text description.
4. If no `vision_provider` is set → the user sees an error and is told to configure one.

### Vision-capable providers

| Provider | Vision support | Recommended model |
|----------|---------------|-------------------|
| OpenAI | ✅ | `gpt-4o` |
| Anthropic | ✅ all Claude 3+ | `claude-sonnet-4-6` |
| Google | ✅ all Gemini | `gemini-2.0-flash` |
| OpenRouter | ✅ varies | `openai/gpt-4o` |
| DeepSeek | ❌ | — |
| Local | depends on model | — |

### Image persistence in chat

Attached images are saved with the session message and are visible after page reload regardless of which model processed them. Image data is stored in the message `metadata` and reconstructed as data URIs when serving session history.

## Troubleshooting

### Models Not Loading?

```
Error fetching models: Timeout
```

**Solution:**
- Check internet connection
- API might be slow/overloaded
- Fallback static list will be used automatically

### Custom Model Not Working?

```
Error: Model not found
```

**Solution:**
1. Verify model ID with provider docs
2. Check if model requires special access
3. Ensure API key has permission for that model

### Too Many Models?

OpenRouter returns 100+ models. We limit display to:
- OpenAI: Top 15 (most recent)
- OpenRouter: Top 50
- Others: All available

Use "Enter custom model ID" for unlisted models.

## Future Enhancements

Potential improvements:
- [ ] Model caching (1 hour TTL)
- [ ] Model metadata (pricing, context size)
- [ ] Model search/filter
- [ ] Model recommendations based on task
- [ ] Favorite models list

## API Rate Limits

Dynamic fetching makes API calls:
- **OpenAI**: `/v1/models` (no cost, part of free tier)
- **Google**: `/v1beta/models` (no cost)
- **OpenRouter**: `/v1/models` (no cost)

These endpoints are **free** and don't count toward usage quotas.

---

**Note:** Web UI fetches provider model lists dynamically. Some CLI/provider-manager paths may still rely on legacy static fallback lists.

**Last Updated:** March 2026
