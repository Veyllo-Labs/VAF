# VAF Sound Assets

This directory contains audio files used for feedback and notifications within the VAF framework, particularly for speech-related actions.

## Contents

- **sst.mp3**: Audio feedback played after successful Speech-to-Text (STT) capture.
- **tts01.mp3**: Notification sound played when the agent has finished processing and an answer is ready.

## Usage

These sounds are managed by the `vaf.core.speech.SpeechManager` and are triggered automatically during voice interactions or background task completions.

## Standards

- Audio files should be in MP3 format.
- Keep volume levels normalized to ensure a consistent user experience.
- Sounds should be brief and non-intrusive.
