# Deeper diagnosis

- At each boundary, identify the input, output, assumed invariant, and point where the observation diverges.
- Reduce to a minimal reproducer, but preserve the condition that makes it fail.
- Use git history as a clue, not as proof of cause.
- Distinguish a failed assertion from infrastructure or environment failure.
- A workaround may be acceptable under urgency; describe the underlying unknown and follow-up.

For BrainFrog: inspect parsing of `/command`, `@file`, or `! shell`; if an argument is lost, compare tokenization, dispatch, and tool call boundaries before changing the renderer.
