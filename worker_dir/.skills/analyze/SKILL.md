# SKILL: structured-analysis

An example Worker skill. Copy this folder to add more skills (e.g.
`.skills/summarize/SKILL.md`, `.skills/refactor/SKILL.md`). The Worker reads the
skill whose purpose matches the Orchestrator's prompt and applies it.

## When to use
Use this skill when the prompt asks you to **analyze**, **assess**, **review**,
or otherwise turn some input text/data into a structured breakdown.

## Input expectations
- A block of text, data, code, or a prior result to analyze.
- An optional target format from the Orchestrator. If the Orchestrator specifies
  a format, that format **wins** over the default below.

## Rules to apply
1. Be concrete and specific — reference the actual input, not generic advice.
2. Prefer structured output (numbered points / tables) over prose walls.
3. State any assumption you made in one short line at the end of `result`.
4. Do not invent facts that are not supported by the input; if the input is
   insufficient, say so in `message` and still deliver a best-effort `result`.
5. Keep it tight: no filler, no restating the prompt back.

## Default output shape (put this inside the contract's `result` field)
A markdown document with:
- **Summary** — 1-2 sentences.
- **Key points** — a numbered list (3-7 items).
- **Risks / caveats** — a short bulleted list (may be "none identified").
- **Recommendation** — 1 sentence.

## Required envelope
Whatever you produce, you MUST still end your reply with the single fenced
` ```json ` Worker contract block from the rules file (`{status, result,
artifacts, message}`), with your analysis placed in `result`. Nothing after the
JSON block.

### Example
```json
{
  "status": "ok",
  "result": "**Summary** — The log shows repeated auth failures from one IP.\n\n**Key points**\n1. 42 failed logins in 5 min\n2. All from 10.0.0.9\n3. Targeting the `admin` account\n\n**Risks / caveats**\n- Possible brute-force; no successful login observed.\n\n**Recommendation** — Rate-limit and block the source IP.\n\n_Assumption: timestamps are UTC._",
  "artifacts": [],
  "message": ""
}
```
