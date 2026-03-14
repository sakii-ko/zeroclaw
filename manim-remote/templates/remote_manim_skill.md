---
name: manim-render
description: Create or revise Manim Community math animations on a configured remote host using the provided render script and fixed output files.
---

Use this skill when the task is to create, revise, or debug a Manim Community animation on a configured remote host.

Workflow:
1. Read `brief.md` first.
2. Prefer editing `scene.py` in the current job directory.
3. Use the provided `scripts/render_manim.sh` helper instead of inventing a custom render command.
4. Keep the result self-contained and avoid downloading extra assets unless the brief clearly needs them.
5. Write the final deliverables to these exact paths:
   - `exports/final.mp4`
   - `exports/summary.md`
   - `exports/result.json`

Expected `exports/result.json` shape:
```json
{
  "status": "completed",
  "scene": "SceneName",
  "quality": "m",
  "video": "exports/final.mp4",
  "summary": "Short plain-language summary"
}
```

Rules:
- Use the preconfigured Manim environment through `scripts/render_manim.sh`.
- Prefer simple native Manim shapes, `MathTex`, `Tex`, `Axes`, `NumberPlane`, and standard animations.
- Aim for a short, readable animation unless the brief asks for a longer one.
- If a render fails, fix the code and rerun instead of stopping at the first error.
- Do not leave the only output buried under Manim's default media tree; copy the final video to `exports/final.mp4`.
