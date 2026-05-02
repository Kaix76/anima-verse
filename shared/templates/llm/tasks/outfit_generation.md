---
task: outfit_generation
purpose: Generate a coherent outfit as a list of clothing pieces (outfit_creation_skill)
placeholders:
  character_name: Character name
  personality: Character personality (or "(not specified)")
  appearance: Character appearance (or "(not specified)")
  context_block: Pre-formatted context (location, activity, mood, weather, etc.)
  hint_block: Optional pre-formatted "User hint: ..." line — empty if none
  existing_block: Pre-formatted existing wardrobe pieces with [EQUIPPED] markers
  type_hint: Pre-formatted dress-code guidance (target_type or generic)
  required_block: Pre-formatted required-slots block (empty if none)
  max_pieces: Max number of pieces
  allowed_slots: Comma-separated list of valid slots
  language_hint: Optional "Use <lang> for the `name` field." — empty if English
---
## system

## user
You are designing individual clothing pieces for a character.

Character: {{ character_name }}
Personality: {{ personality }}
Appearance: {{ appearance }}

Context:
{{ context_block }}

{% if hint_block %}{{ hint_block }}
{% endif %}
Existing pieces in the character's wardrobe. REUSE these by using the EXACT same `name` and `slot` — the system will deduplicate and skip creating a new item. Only generate a NEW piece when nothing in the wardrobe fits the context.
{{ existing_block }}

{{ type_hint }}

{{ required_block }}

Generate a COHERENT OUTFIT as a list of individual pieces. Rules:
- Return at most {{ max_pieces }} pieces.
- Each piece has a `slots` array — list every slot the garment physically occupies. Allowed: {{ allowed_slots }}
- Only generate required slots that are NOT already marked [EQUIPPED] above — an equipped piece in a slot satisfies that slot and should not be duplicated.
- Add outer (jacket/coat) only when context (weather/activity) fits.
- head/neck/legs/underwear_* only if the style demands them (e.g. sleepwear, sportswear) or required above.
- prompt_fragment is the concrete visual description used for image generation
  (e.g. "black leather moto jacket, silver zippers"). NO character names, NO poses.
- name is short, 2-4 words (e.g. "Black Leather Jacket"). {{ language_hint }}
- outfit_types is an array of tags (e.g. ["Casual"], ["Business","Formal"]).

SLOT semantics (MANDATORY — place each garment in the correct slot):
- head: hats, caps, headbands, hairbands, fascinators.
- neck: necklaces, chokers, scarves, ties, bowties.
- outer: jackets, coats, blazers, cardigans, hoodies, vests, boleros.
- top: shirts, blouses, t-shirts, crop tops, tank tops, bras (as outerwear),
  bodies, bodysuits, dress tops, corsets, bustiers.
- bottom: pants, jeans, skirts, shorts, hot-pants, leggings WORN AS PANTS, mini-skirts.
- underwear_top: bra, sports-bra (under clothing), bralette, nipple-covers.
- underwear_bottom: panties, thong, briefs, boxers, G-string.
- legs: stockings, pantyhose, tights, knee-high socks, garters, leg-warmers.
- feet: shoes, boots, sneakers, heels, sandals, flip-flops, loafers.

CRITICAL: "Jacket" → slots: ["outer"]. "Leather Jacket" → slots: ["outer"].
"Dress" → slots: ["top", "bottom"]. NEVER put outerwear in `bottom`.

Multi-slot pieces: if one garment physically occupies multiple slots (e.g. a
dress covers top+bottom, a jumpsuit covers top+bottom+legs, thigh-high
stockings cover legs+feet), emit ONE piece and list ALL its slots in `slots`.
Do NOT create a second piece for one of those slots — the multi-slot piece
already occupies them.

Visual coverage (optional): when another piece is only visible because it's
layered on top, use:
- `covers`: slots fully hidden behind this piece (dropped from the prompt)
- `partially_covers`: slots partially visible as "underneath" (rendered as
  "{fragment} underneath {covering-fragment}")
covers/partially_covers must NOT include any slot from this piece's own `slots`.

Return ONLY valid JSON with this schema:
{
  "pieces": [
    {"slots": ["top", "bottom"], "name": "...", "prompt_fragment": "...",
      "outfit_types": ["..."], "covers": [], "partially_covers": []},
    ...
  ]
}
