Wake-word training data layout

Put recorded WAV files here before training an openwakeword model.

Structure:

- data/positive/<label>/sample_0001.wav  # positive examples for label (e.g. jarvis)
- data/negative/sample_0001.wav          # negative/background audio

Recommendations:
- Collect 200+ positive samples per wake phrase across different speakers,
  microphones, intonations and noise conditions.
- Collect several minutes/hours of negative audio: ambient room noise, music,
  other speech, TV, etc.
- Use `python scripts/record_samples.py --mode positive --label jarvis --count 300 --duration 1.2` to capture positives.
- Use `python scripts/record_samples.py --mode negative --count 600 --duration 1.2` to capture negatives.

After collecting data, follow `openwakeword` training docs to train models and
place them in your OpenWakeWord models directory so the app can load them.
