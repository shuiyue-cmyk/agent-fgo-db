---
name: fgo-wiki-kb
description: Answer factual Fate/Grand Order (FGO) questions from a local, searchable SQLite copy of the Mooncell wiki (fgo.wiki). Use for Servant stats, skills, Noble Phantasms, ascension and skill materials, Craft Essences, quests, enemies, events, summon banners, voice lines and lore, in Chinese, Japanese or English (从者、技能、宝具、素材、礼装、关卡、活动、卡池).
compatibility: Requires Python 3.11+ (standard library only). Building or refreshing the database needs network access to fgo.wiki.
---

# FGO Wiki Knowledge Base

Use the local Mooncell (fgo.wiki) database as the factual source for Fate/Grand Order questions. Search its index first, then read the matching page or section before answering. Do not answer FGO facts from memory when the database can be checked.

## Location

Everything lives in the skill directory, the folder that contains this `SKILL.md`:

- Tool: `fgo_kb.py`
- Database: `fgo_wiki.sqlite3`, created next to the tool by the first sync. It is not shipped with the skill.

Run every command below from the skill directory, or pass the tool's absolute path. Use `python3` where `python` is not Python 3. To keep the database elsewhere, set the `FGO_KB_DB` environment variable to its path or pass `--db <path>` before the subcommand. Output is UTF-8; the script configures that itself.

## First-time setup

Check that the database exists before the first lookup:

```
python fgo_kb.py stats
```

If it reports `Database not found`, the database has to be built once. Tell the user this takes about half an hour, makes a few hundred low-rate requests to fgo.wiki and produces a file of roughly 600 MiB, then run it in the background if they agree:

```
python fgo_kb.py sync --full --delay 0.5
```

If that run is interrupted, continue it with `sync --full --resume --delay 0.5`. Until the database exists, fall back to a live lookup on fgo.wiki and say so.

## Lookup workflow

1. Turn the question into a few distinctive Chinese or English search terms: the character or item name plus the subject, such as `"摩根 技能"`, `"龙之牙 获取"` or `"阿尔托莉雅 宝具 倍率"`. Page titles use Simplified Chinese; nicknames and Japanese names usually resolve through redirects.
2. Search:

   ```
   python fgo_kb.py search "关键词" --limit 12 --format json
   ```

3. Prefer the canonical page title and read the relevant section:

   ```
   python fgo_kb.py show "页面标题" --section "章节名" --format json
   ```

   Omit `--section` for the whole page. Add `--raw` when the cleaned text hides template semantics or table structure. Long pages are cut at `--max-chars` (default 20000) and marked `[TRUNCATED]`; raise the limit or narrow the section.
4. For Servant base stats and other common template data, read the structured fields:

   ```
   python fgo_kb.py fields "从者名"
   ```

5. If a title is ambiguous, inspect `redirect_chain`, the categories and the matching titles before choosing a page.

## Team-cost rules

- A normal battle party has six combat slots: five Servants owned by the player and one support Servant supplied by a friend or the game.
- Only the player's five owned Servants and their equipped Craft Essences consume the player's party COST. The support slot does not.
- Bond points earned by the support belong to the support's owner. Leave the support Servant out when optimising the player's own bond gain.
- Confirm special formation rules on the specific quest page. For Grand Servant stages, use the quest page and the `冠位肖像` rules for the dedicated GRAND SELECT slot and any COST-free Craft Essence slots.

## Freshness and sources

- Every result carries `revision_timestamp`. For current availability, event status, maintenance or newly released content, refresh first:

  ```
  python fgo_kb.py sync
  ```

  This fetches only pages changed since the last sync and usually finishes in under a minute. Use `sync --full --resume --delay 0.5` when the database has no usable checkpoint, or when the user asks for a complete refresh.
- The Chinese (国服) and Japanese (日服) servers run on different schedules. State which server a date or availability claim refers to.
- Answer with the concrete facts the user asked for. Cite the source page URL, and give the revision date when freshness matters.
- Synced content is CC BY-NC-SA 4.0 and meant for personal, non-commercial lookup. Quote only what the answer needs and keep the source link.
- If the database does not contain a fact, say what is missing and fall back to a live lookup on fgo.wiki instead of guessing.
