---
name: h3-pv-anime
description: Generate MiniMax H3 anime PV image-to-video prompts from a reference image using the supplied director-led template. Use for anime PV, promotional PV, flash, combat, epic narrative, or adult glamour PV requests; not for generic H3 video prompting.
---

# H3 · 动漫 PV

Use [动漫PV模板.md](references/动漫PV模板.md) as the authoritative writing specification. Read it in full before producing a prompt.

## Workflow

Silently inspect the reference image, select the appropriate primary PV branch, develop three meaningfully different directing concepts, select the strongest image-grounded concept, construct a feasible shot plan, and run the template's quality gates.

Respect the template's identity, first-frame, timing, safety, complexity, and anti-cliché requirements. For a bare “快闪PV” request, use its high-budget dynamic-layout default; choose the flash engine from the image rather than a fixed shot list.

## Output

Unless the user requests planning or comparison, output only one final English H3 I2VA prompt in exactly this structure:

```text
For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced.

integrated_multimodal_description: ...

overall_soundscape: ...

non_diegetic_music: ...
```

Use strictly increasing timestamps after Shot 1. Keep the prompt executable and do not reveal internal evidence cards, concept candidates, scoring, or Chinese drafting notes.
