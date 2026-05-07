---
name: ChangeOutfit
---
Equips outfit pieces from the character's inventory. You do NOT need to specify items: calling this with an empty input automatically picks pieces matching the current location and activity (and falls back to OutfitCreation if the inventory lacks suitable pieces). Optional input: JSON {"equip": ["piece-id-or-name", ...], "unequip_slots": ["outer", ...], "unequip_items": ["item-id", ...], "outfit_type": "Casual|Business|..."} OR free-text piece names. Do not invent items not in the inventory — leave input empty and the skill will choose appropriate pieces.
