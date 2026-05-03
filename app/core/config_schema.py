"""Config schema definition for admin settings page.

Each section defines field metadata: type, label, description, default,
sensitive flag, choices, and validation rules. This drives both the
admin UI rendering and server-side validation.
"""

# Field types: str, int, float, bool, text (multiline), select, password, json_str
# "array" and "object" are handled by section-level definitions

SECTIONS = {
    "server": {
        "label": "Server",
        "icon": "⚙",
        "fields": {
            "log_level": {
                "type": "select",
                "label": "Log Level",
                "choices": ["DEBUG", "INFO", "WARNING", "ERROR"],
                "default": "INFO",
            },
            "jwt_secret": {
                "type": "password",
                "label": "JWT Secret",
                "description": "Secret key for JWT token signing. Change in production!",
                "sensitive": True,
            },
            "storage_dir": {
                "type": "str",
                "label": "Storage Directory",
                "default": "./storage",
                "description": "Basisverzeichnis fuer Datenbanken, Configs und Uploads",
            },
        },
    },
    "beszel": {
        "label": "Beszel GPU Monitoring",
        "icon": "📊",
        "fields": {
            "url": {"type": "str", "label": "Beszel URL", "placeholder": "http://host:8090", "description": "Fuer intelligentes VRAM-Management: Models nur entladen wenn VRAM knapp"},
            "email": {"type": "str", "label": "E-Mail"},
            "password": {"type": "password", "label": "Passwort", "sensitive": True},
        },
    },
    "providers": {
        "label": "LLM Providers",
        "icon": "🤖",
        "is_array": True,
        "item_label_field": "name",
        "fields": {
            "name": {"type": "str", "label": "Name", "required": True, "description": "Eindeutiger Name (wird in Task Defaults und GPU-Zuordnung referenziert)"},
            "type": {
                "type": "select",
                "label": "Typ",
                "choices": ["openai", "ollama", "anthropic"],
                "default": "openai",
                "description": "API-Protokoll des Providers",
            },
            "api_base": {"type": "str", "label": "API Base URL", "required": True, "placeholder": "http://host:port/v1"},
            "api_key": {"type": "password", "label": "API Key", "sensitive": True, "default": "not-needed", "description": "API Key (bei lokalen Providern: 'not-needed')"},
            "timeout": {"type": "int", "label": "Timeout (s)", "default": 120, "min": 10, "max": 3600, "description": "Request Timeout in Sekunden"},
            "max_concurrent": {"type": "int", "label": "Max Concurrent", "default": 1, "min": 1, "max": 50, "description": "Maximale gleichzeitige Anfragen"},
            "beszel_system_id": {"type": "str", "label": "Beszel System-ID", "description": "System-ID fuer GPU VRAM-Ueberwachung via Beszel"},
            "gpus": {
                "type": "array",
                "label": "GPUs",
                "item_fields": {
                    "label": {"type": "str", "label": "Label", "description": "Anzeigename der GPU (z.B. 'RTX 4090 #1')"},
                    "vram_gb": {"type": "int", "label": "VRAM (GB)", "min": 0, "max": 512},
                    "device": {"type": "str", "label": "Device", "default": "0", "description": "Beszel GPU-Key (optional, fuer Monitoring)"},
                    "types": {
                        "type": "str",
                        "label": "Nutzung",
                        "description": "Kommasepariert: ollama, openai, comfyui",
                        "default": "openai",
                    },
                    "max_concurrent": {"type": "int", "label": "Max Concurrent", "default": 1, "min": 1, "max": 50, "description": "Max gleichzeitige Aufgaben auf dieser GPU"},
                },
            },
        },
    },
    "llm_routing": {
        "label": "LLM Routing",
        "icon": "🧭",
        "is_array": True,
        "item_label_field": "model",
        "fields": {
            "provider": {"type": "provider_select", "label": "Provider", "required": True},
            "model": {"type": "model_select", "label": "Model", "required": True},
            "temperature": {
                "type": "float",
                "label": "Temperature",
                "default": 0.7,
                "min": 0,
                "max": 2,
                "step": 0.1,
                "description": "Recommended by task category — Tools: 0.0-0.2 · Image: 0.2-0.4 · Helper: 0.3-0.6 · Chat: 0.7-0.9",
            },
            "max_tokens": {"type": "int", "label": "Max Tokens", "min": 0, "max": 100000},
            "chat_template": {
                "type": "text",
                "label": "Chat Template (optional)",
                "description": "Jinja chat_template — only set if the provider's tokenizer has no default template (some Infermatic / vLLM finetunes since transformers v4.44). Sent via extra_body.chat_template. Leave empty to use the provider default.",
            },
            "tasks": {
                "type": "task_order_list",
                "label": "Tasks",
                "description": "Tasks this LLM serves. Order is the fallback rank between LLMs that share the same task (1 = primary, 2 = fallback if primary unavailable). Use the + All <category> buttons below to bulk-add a whole task group.",
            },
        },
    },
    "memory": {
        "label": "Memory / Gedaechtnis",
        "icon": "🧠",
        "fields": {
            "short_term_days": {"type": "int", "label": "Kurzzeit (Tage)", "default": 3, "min": 1, "max": 14, "description": "Chat-History im Prompt (Stufe 1). Ab diesem Alter werden Episodics zu Tages-Summaries konsolidiert."},
            "mid_term_days": {"type": "int", "label": "Mittelzeit (Tage)", "default": 30, "min": 7, "max": 180, "description": "Ab diesem Alter werden Tages-Summaries zu Wochen-Summaries konsolidiert (Stufe 2 → 3)."},
            "long_term_days": {"type": "int", "label": "Langzeit (Tage)", "default": 90, "min": 30, "max": 365, "description": "Ab diesem Alter werden Wochen-Summaries zu Monats-Summaries konsolidiert."},
            "max_messages": {"type": "int", "label": "Max Nachrichten", "default": 100, "min": 10, "max": 500, "description": "Safety-Cap: Maximale Anzahl Chat-Nachrichten im Prompt."},
            "session_gap_hours": {"type": "int", "label": "Session-Bruch (Stunden)", "default": 4, "min": 0, "max": 24, "description": "Zeitluecke zwischen Turns, ab der die Chat-History abgeschnitten wird — Turns vor der letzten solchen Luecke wandern in die Session-Summary. 0 = deaktiviert."},
            "max_semantic": {"type": "int", "label": "Max Fakten", "default": 50, "min": 10, "max": 200, "description": "Maximale Anzahl semantischer Memories pro Character (Hard-Cap)."},
            "commitment_max_days": {"type": "int", "label": "Commitment Max-Alter (Tage)", "default": 5, "min": 1, "max": 30, "description": "Offene Commitments ohne 'completed'/'important'-Tag und importance<4 werden nach diesem Alter beim Cleanup entfernt."},
            "commitment_completed_days": {"type": "int", "label": "Erledigtes Commitment (Tage)", "default": 3, "min": 1, "max": 14, "description": "Erledigte Commitments (Tag 'completed') werden nach diesem Alter entfernt."},
        },
    },
    "image_generation": {
        "label": "Image Generation",
        "icon": "🎨",
        "fields": {
            "enabled": {"type": "bool", "label": "Aktiviert", "default": True},

            # --- Default-Backends ---
            "comfy_default_workflow": {"type": "workflow_select", "label": "Default ComfyUI Workflow", "description": "Standard-Workflow fuer normale Bildgenerierung"},
            "outfit_imagegen_default": {"type": "imagegen_select", "label": "Outfit/Vorschau Default Backend", "description": "Backend fuer Garderobe-Vorschau + Outfit-Bilder"},
            "expression_imagegen_default": {"type": "imagegen_select", "label": "Expression Default Backend", "description": "Backend fuer Mood/Activity-basierte Varianten"},
            "location_imagegen_default": {"type": "imagegen_select", "label": "Location Default Backend"},

            # --- Prompt-Prefixes ---
            "profile_image_prompt_prefix": {"type": "str", "label": "Profil-Bild Prompt Prefix", "default": "photorealistic, portrait, only head,", "description": "Wird Profilbild-Prompts vorangestellt (z.B. 'photorealistic, portrait')"},
            "outfit_image_prompt_prefix": {"type": "str", "label": "Outfit/Vorschau Prompt Prefix", "default": "full body portrait", "description": "Wird Garderobe-Vorschau-Prompts vorangestellt (z.B. 'full body portrait, RAW photo'). Nur fuer Vorschau, nicht fuer Expression-Auto-Regen."},

            # --- Outfit-Bild Groesse ---
            "outfit_image_width": {
                "type": "int",
                "label": "Outfit Breite (px)",
                "default": 832,
                "min": 64,
                "max": 4096,
                "description": (
                    "Hochformat (~2:3) fuer Ganzkoerper-Outfits. Render-Zeit skaliert grob "
                    "linear mit der Pixelanzahl, deshalb sind nur Sprünge mit deutlichem "
                    "Performance-Effekt sinnvoll:\n"
                    "  - 640x960   (~0.6 MP — ca. 60% Zeit, fuer schnelle Iteration/Vorschau)\n"
                    "  - 832x1216  (~1.0 MP — Default, SDXL-/Flux-Sweet-Spot)\n"
                    "  - 1024x1536 (~1.6 MP — ca. 60% mehr Zeit/VRAM, mehr Detail)\n"
                    "Darüber hinaus (z.B. 1280x1920) waechst die Zeit weiter linear, der "
                    "Qualitaetsgewinn flacht aber ab und Repetitionsartefakte werden "
                    "wahrscheinlicher — fuer scharfe grosse Bilder besser einen Upscale-Pass "
                    "nachschalten."
                ),
            },
            "outfit_image_height": {
                "type": "int",
                "label": "Outfit Hoehe (px)",
                "default": 1216,
                "min": 64,
                "max": 4096,
                "description": "Siehe Outfit Breite fuer empfohlene Bucket-Kombinationen im selben Verhaeltnis.",
            },

            # --- Sonstiges ---
            "u2net_home": {"type": "str", "label": "U2Net Model Path", "default": "./models/u2net", "description": "Pfad fuer u2net-Modell (Hintergrundentfernung via rembg)"},
            "image_analysis_prompt": {
                "type": "text",
                "label": "Image Analysis Prompt",
                "description": "System prompt for objective post-generation image analysis (vision LLM, task image_analysis).",
                "default": (
                    "You are an expert image analyst. Provide a detailed and objective "
                    "description of the image (max 300 tokens) in flowing prose. Cover:\n"
                    "1. Overall scene: setting, environment, lighting, mood, composition.\n"
                    "2. Subjects: number of people, body type, hair, skin tone, facial "
                    "expression, pose.\n"
                    "3. Clothing: garments in detail (style, color, fit, condition).\n"
                    "4. Actions and interactions: what is happening, body positions.\n"
                    "5. Visual style: photographic, illustrated, 3D render, anime, etc. "
                    "Camera angle, depth of field, color palette.\n\n"
                    "Rules:\n"
                    "- Respond in fluent, descriptive English prose.\n"
                    "- If something is ambiguous or partially visible, describe it as such.\n"
                    "- Plain text only — no markdown."
                ),
            },
            "rebuild_llm_system_template": {
                "type": "text",
                "label": "Rebuild LLM System Prompt",
                "default": (
                    "You are an image prompt enhancer for the {target_model} image model. "
                    "{prompt_instruction} "
                    "Rewrite the following prompt in the style requested, keeping ALL factual content "
                    "(persons, outfits, pose, expression, scene, location, mood). "
                    "Do NOT add new visual elements, do NOT remove any. "
                    "Respond with ONLY the rewritten prompt, no preamble, no commentary."
                ),
                "description": "System-Prompt fuer den Image-Prompt-Enhancer LLM. Platzhalter: {target_model} (z_image/qwen/flux), {prompt_instruction} (aus Workflow-Config).",
            },
        },
        "sub_arrays": {
            "backends": {
                "label": "Backends",
                "item_label_field": "name",
                "sort_alphabetically": True,
                "fields": {
                    "name": {"type": "str", "label": "Name", "required": True},
                    "enabled": {"type": "bool", "label": "Aktiviert", "default": True},
                    "api_type": {
                        "type": "select",
                        "label": "API Typ",
                        "choices": ["a1111", "comfyui", "mammouth", "civitai", "together"],
                    },
                    "api_url": {"type": "str", "label": "API URL"},
                    "api_key": {"type": "password", "label": "API Key", "sensitive": True, "description": "Erforderlich fuer Cloud-Backends (mammouth, civitai, together)"},
                    "model": {"type": "str", "label": "Model", "description": "Modell-ID oder URN (civitai: urn:air:sdxl:checkpoint:...)"},
                    "cost": {"type": "int", "label": "Kosten", "default": 0, "min": 0, "description": "Relative Kosten (0 = lokal/kostenlos, hoeher = teurer)"},
                    "fallback_mode": {
                        "type": "select",
                        "label": "Fallback-Strategie",
                        "default": "next_cheaper",
                        "choices": ["none", "next_cheaper", "specific"],
                        "description": "Was tun wenn dieses Backend nicht verfuegbar ist? none = Fehler, next_cheaper = naechst-billigeres aktives Backend, specific = explizites Backend",
                    },
                    "fallback_specific": {
                        "type": "imagegen_backend_select",
                        "label": "Fallback-Backend (specific)",
                        "description": "Nur relevant wenn Fallback-Strategie = specific. Backend das uebernimmt wenn dieses ausfaellt — kann anderer api_type/Workflow sein (z.B. Qwen-Backend down -> Together-Flux).",
                    },
                    "width": {
                        "type": "int",
                        "label": "Breite",
                        "default": 800,
                        "min": 64,
                        "max": 4096,
                        "description": (
                            "Quadratisches Default-Format (~1:1). Render-Zeit skaliert grob "
                            "linear mit der Pixelanzahl, deshalb sind nur Sprünge mit "
                            "deutlichem Performance-Effekt sinnvoll:\n"
                            "  - 512x512   (~0.26 MP — ca. 30% Zeit, schnelle Iteration)\n"
                            "  - 768x768   (~0.59 MP — ca. 70% Zeit, brauchbare Details)\n"
                            "  - 1024x1024 (~1.05 MP — SDXL/Flux Sweet-Spot)\n"
                            "  - 1280x1280 (~1.64 MP — ca. 60% mehr Zeit/VRAM)\n"
                            "Default 800x800 liegt zwischen den Buckets — fuer beste "
                            "Resultate auf einen der oben genannten setzen. Ueber 1MP waechst "
                            "die Zeit weiter linear, der Qualitaetsgewinn flacht aber ab — "
                            "fuer scharfe grosse Bilder besser einen Upscale-Pass nachschalten."
                        ),
                    },
                    "height": {
                        "type": "int",
                        "label": "Höhe",
                        "default": 800,
                        "min": 64,
                        "max": 4096,
                        "description": "Siehe Breite fuer empfohlene Bucket-Kombinationen.",
                    },
                    "faceswap_needed": {"type": "bool", "label": "FaceSwap nötig", "default": False, "description": "Generierte Bilder benoetigen FaceSwap als Post-Processing"},
                    "prompt_prefix": {"type": "str", "label": "Prompt Prefix"},
                    "negative_prompt": {"type": "str", "label": "Negative Prompt"},
                    "guidance_scale": {"type": "float", "label": "Guidance Scale", "min": 0, "max": 50, "step": 0.5},
                    "num_inference_steps": {"type": "int", "label": "Inference Steps", "min": 1, "max": 200},
                    "vram_required": {"type": "int", "label": "VRAM Required (GB)", "min": 0, "max": 512, "description": "GB VRAM die dieses Backend braucht"},
                    "disable_safety": {"type": "bool", "label": "Safety deaktivieren", "default": False},
                    "poll_interval": {"type": "float", "label": "Poll Interval (s)", "default": 3.0, "min": 0.5, "step": 0.5, "description": "Nur fuer async Cloud-Backends (CivitAI, Together): Wartezeit zwischen Status-Polls. Niedriger = schnelleres Erkennen wenn Bild fertig (aber mehr API-Calls)."},
                    "max_wait": {"type": "int", "label": "Max Wait (s)", "default": 300, "min": 30, "description": "Nur fuer async Cloud-Backends: Maximale Wartezeit bis Generation als fehlgeschlagen gilt."},
                },
            },
            "comfyui_workflows": {
                "label": "ComfyUI Workflows",
                "is_dict": True,
                "item_label_field": "name",
                "sort_alphabetically": True,
                "fields": {
                    "name": {"type": "str", "label": "Anzeigename", "required": True},
                    "filter": {"type": "str", "label": "Filter Pattern", "description": "Glob-Pattern zum Filtern von Modellen/LoRAs (* als Wildcard, case-insensitive)"},
                    "skill": {"type": "comfyui_backend_select", "label": "Backend(s)", "description": "ComfyUI Backend das diesen Workflow ausfuehren kann (leer = alle)"},
                    "workflow_file": {"type": "str", "label": "Workflow Datei", "required": True},
                    "faceswap_needed": {"type": "bool", "label": "FaceSwap nötig", "default": False},
                    "model": {"type": "comfyui_model_select", "label": "Model"},
                    "clip": {"type": "comfyui_clip_select", "label": "CLIP Model"},
                    "prompt_style": {"type": "text", "label": "Prompt Style", "default": "photorealistic", "description": "Stil-Adjektiv / Style-Keywords. Erscheint im Summary ('A {erstes Wort} group photo of...') und komplett in der Style-Zeile. Default: photorealistic."},
                    "prompt_negative": {"type": "text", "label": "Negative Prompt"},
                    "image_model": {"type": "select", "label": "Target Prompt Stil", "choices": ["", "z_image", "qwen", "flux"], "description": "Bestimmt den Prompt-Adapter (z_image=Komma-Keywords, qwen=natuerliche Saetze, flux=Fotografie-Stil). Leer = Fallback ueber Workflow-Dateiname."},
                    "prompt_instruction": {"type": "text", "label": "Prompt Instruction (Enhancer)", "description": "Optional: Anweisung fuer den Enhancer-LLM (task=image_prompt). Leer = Template-Output wird direkt verwendet (schnell, deterministisch)."},
                    "vram_required": {"type": "int", "label": "VRAM Required (GB)", "min": 0, "description": "Ueberschreibt Backend-Default"},
                    "width": {
                        "type": "int",
                        "label": "Breite",
                        "default": 800,
                        "min": 64,
                        "max": 4096,
                        "description": (
                            "Quadratisches Default-Format (~1:1) fuer den Workflow. "
                            "Performance-relevante Stufen (Render-Zeit ≈ linear zu MP):\n"
                            "  - 512x512   (~0.26 MP — ca. 30% Zeit, schnelle Iteration)\n"
                            "  - 768x768   (~0.59 MP — ca. 70% Zeit)\n"
                            "  - 1024x1024 (~1.05 MP — SDXL/Flux Sweet-Spot)\n"
                            "  - 1280x1280 (~1.64 MP — ca. 60% mehr Zeit/VRAM)\n"
                            "Workflow-Override schlaegt Backend-Default. Default 800x800 liegt "
                            "zwischen den SDXL-Buckets — fuer beste Resultate auf 768 oder 1024 "
                            "setzen. Ueber 1MP flacht der Qualitaetsgewinn ab und "
                            "Repetitionsartefakte werden wahrscheinlicher — Upscale-Pass "
                            "nachgelagert ist meist effizienter."
                        ),
                    },
                    "height": {
                        "type": "int",
                        "label": "Höhe",
                        "default": 800,
                        "min": 64,
                        "max": 4096,
                        "description": "Siehe Breite fuer empfohlene Bucket-Kombinationen.",
                    },
                    "loras": {
                        "type": "lora_array",
                        "label": "LoRAs",
                        "max_items": 4,
                    },
                    "fallback_specific": {
                        "type": "imagegen_backend_select",
                        "label": "Workflow-Fallback (override)",
                        "description": "Optional: Backend das uebernimmt wenn das Primaer-Backend fuer DIESEN Workflow ausfaellt. Ueberschreibt die Backend-eigene fallback_specific-Einstellung — so kann z.B. Z-Image auf Together und Qwen auf ein anderes Backend gehen, obwohl beide auf ComfyUI-3090 laufen.",
                    },
                },
            },
        },
    },
    "faceswap": {
        "label": "FaceSwap / MultiSwap",
        "icon": "🎭",
        "fields": {
            "default_swap_mode": {"type": "select", "label": "Standard Swap-Modus", "default": "comfyui", "choices": ["internal", "comfyui", "multiswap"], "description": "Server-weiter Default wenn Character 'Server-Einstellung' gewaehlt hat (internal=Face Service, comfyui=ReActor, multiswap=Flux.2)"},
            "_grp_face_swap": {"type": "group_header", "label": "Face Swap"},
            "comfy_workflow_file": {"type": "str", "label": "ComfyUI FaceSwap Workflow", "description": "Workflow-Datei mit ReActor-Kette"},
            "comfy_backend": {"type": "comfyui_backend_select", "label": "ComfyUI FaceSwap Backend", "description": "Leer = Auto (dynamisches Routing zum besten ComfyUI-Kanal)"},
            "comfy_vram_required": {"type": "int", "label": "VRAM Required (GB)", "default": 8, "min": 0},
            "_grp_multi_swap": {"type": "group_header", "label": "Multi Swap"},
            "multiswap_workflow_file": {"type": "str", "label": "MultiSwap Workflow", "default": "./workflows/multiswap_flux2_api.json", "description": "Flux.2 MultiSwap-Workflow (Face+Hair Swap als Alternative zu ReActor)"},
            "multiswap_unet": {"type": "comfyui_model_select", "label": "MultiSwap UNet (Model)", "description": "UNet-Modell, das beim MultiSwap-Workflow in Node 'input_unet' gesetzt wird (leer = Workflow-Default beibehalten)"},
            "multiswap_clip": {"type": "comfyui_clip_select", "label": "MultiSwap CLIP (Text Encoder)", "description": "CLIP-Modell, das beim MultiSwap-Workflow in Node 'input_clip' gesetzt wird (leer = Workflow-Default beibehalten)"},
            "multiswap_backend": {"type": "comfyui_backend_select", "label": "MultiSwap Backend", "description": "Leer = FaceSwap-Backend"},
            "_grp_face_service": {"type": "group_header", "label": "Face Service"},
            "service_url": {"type": "str", "label": "Face Service URL", "default": "http://localhost:8005", "description": "Fallback: Standalone insightface/inswapper Service"},
            "service_port": {"type": "int", "label": "Face Service Port", "default": 8005},
            "service_model_path": {"type": "str", "label": "Model Path", "description": "inswapper_128.onnx oder reswapper_256.onnx"},
            "service_det_size": {"type": "int", "label": "Detection Size", "default": 640},
            "service_omp_num_threads": {"type": "int", "label": "OMP Threads", "default": 4, "min": 1},
            "service_enabled": {"type": "bool", "label": "Service Aktiviert", "default": True},
            "service_debug": {"type": "bool", "label": "Debug Modus", "default": False},
        },
    },
    "face_enhance": {
        "label": "Face Enhancement",
        "icon": "✨",
        "fields": {
            "enabled": {"type": "bool", "label": "Aktiviert", "default": True, "description": "Face Enhancement global aktivieren/deaktivieren"},
            "model_path": {
                "type": "select",
                "label": "Enhancement Model",
                "choices": ["./models/GFPGANv1.4.onnx", "./models/codeformer.onnx", "./models/GPEN-BFR-512.onnx"],
            },
            "blend": {"type": "float", "label": "Blend", "default": 1.0, "min": 0, "max": 1, "step": 0.1, "description": "0.0 = nur Original, 0.5 = halb-halb, 1.0 = voll enhanced"},
            "codeformer_weight": {"type": "float", "label": "CodeFormer Weight", "default": 0.7, "min": 0, "max": 1, "step": 0.1, "description": "Nur CodeFormer: 0.0 = max Qualitaet, 1.0 = max Identitaet"},
            "color_correction": {"type": "bool", "label": "Farbkorrektur", "default": True, "description": "Lab-Farbraum-Angleichung (Hauttonene anpassen)"},
            "sharpen": {"type": "bool", "label": "Nachschärfung", "default": True, "description": "Unsharp-Mask Nachschaerfung"},
            "sharpen_strength": {"type": "float", "label": "Schärfe Stärke", "default": 0.5, "min": 0.1, "max": 1, "step": 0.1, "description": "0.1 = kaum, 0.5 = moderat, 1.0 = stark"},
        },
    },
    "animation": {
        "label": "Animation (Video)",
        "icon": "🎬",
        "subsections": {
            "comfy": {
                "label": "ComfyUI Animation",
                "fields": {
                    "enabled": {"type": "bool", "label": "Aktiviert", "default": False},
                    "workflow_file": {"type": "str", "label": "Workflow Datei"},
                    "backend": {"type": "comfyui_backend_select", "label": "ComfyUI Backend"},
                    "unet_high": {"type": "comfyui_model_select", "label": "UNet High Lighting"},
                    "unet_low": {"type": "comfyui_model_select", "label": "UNet Low Lighting"},
                    "clip": {"type": "comfyui_clip_select", "label": "CLIP Model"},
                    "width": {"type": "int", "label": "Breite", "default": 640, "min": 64, "max": 4096},
                    "height": {"type": "int", "label": "Höhe", "default": 640, "min": 64, "max": 4096},
                    "poll_interval": {"type": "float", "label": "Poll Interval (s)", "default": 3.0, "min": 0.5, "step": 0.5},
                    "max_wait": {"type": "int", "label": "Max Wait (s)", "default": 600, "min": 60},
                },
            },
            "together": {
                "label": "Together.ai Animation",
                "fields": {
                    "enabled": {"type": "bool", "label": "Aktiviert", "default": False},
                    "label": {"type": "str", "label": "Anzeigename"},
                    "api_key": {"type": "password", "label": "API Key", "sensitive": True, "description": "Leer = wird aus Together-Provider API Key gelesen"},
                    "model": {"type": "str", "label": "Model"},
                    "width": {"type": "int", "label": "Breite", "default": 720, "description": "Kling 2.1: 1280x720, 720x1280 oder 720x720"},
                    "height": {"type": "int", "label": "Höhe", "default": 720},
                    "seconds": {"type": "int", "label": "Dauer (s)", "default": 5, "min": 1, "max": 30},
                    "poll_interval": {"type": "float", "label": "Poll Interval (s)", "default": 5.0, "step": 0.5},
                    "max_wait": {"type": "int", "label": "Max Wait (s)", "default": 600},
                },
            },
        },
    },
    "tts": {
        "label": "Text-to-Speech",
        "icon": "🔊",
        "fields": {
            "enabled": {"type": "bool", "label": "TTS Aktiviert", "default": False},
            "auto": {"type": "bool", "label": "Auto-TTS", "default": False, "description": "Automatisch Audio generieren fuer jede Antwort"},
            "chunk_size": {"type": "int", "label": "Chunk Size (Zeichen)", "default": 300, "min": 0, "description": "Audio ab dieser Zeichenanzahl erzeugen (0 = ein Audio nach komplettem Text)"},
            "backend": {
                "type": "select",
                "label": "Backend",
                "choices": ["xtts", "f5", "magpie", "comfyui"],
                "default": "xtts",
            },
            "fallback_backend": {
                "type": "select",
                "label": "Fallback Backend",
                "choices": ["", "xtts", "f5", "magpie", "comfyui"],
                "default": "",
                "description": "Falls primaeres Backend nicht erreichbar",
            },
        },
        "subsections": {
            "xtts": {
                "label": "XTTS v2",
                "fields": {
                    "url": {"type": "str", "label": "XTTS URL", "default": "http://localhost:8020"},
                    "speaker_wav": {"type": "str", "label": "Speaker WAV", "description": "Eigene WAV oder built-in: calm_female, female, male"},
                    "language": {"type": "str", "label": "Sprache", "default": "de"},
                },
            },
            "magpie": {
                "label": "Magpie (NVIDIA Riva)",
                "fields": {
                    "url": {"type": "str", "label": "Magpie URL", "default": "http://localhost:9000"},
                    "voice": {"type": "str", "label": "Stimme", "description": "Format: Magpie-Multilingual.{LANG}.{Name}[.{Emotion}]"},
                    "language": {"type": "str", "label": "Sprache", "default": "de-DE"},
                },
            },
            "f5": {
                "label": "F5-TTS",
                "fields": {
                    "url": {"type": "str", "label": "F5 URL", "default": "http://localhost:7860"},
                    "ref_audio": {"type": "str", "label": "Referenz Audio", "description": "Pfad zur WAV-Datei fuer Voice Cloning (5-8 Sekunden empfohlen)"},
                    "ref_text": {"type": "str", "label": "Referenz Text", "description": "Transkription des Referenz-Audios (leer = auto-detect)"},
                    "speed": {"type": "float", "label": "Geschwindigkeit", "default": 1.0, "min": 0.1, "max": 3.0, "step": 0.1, "description": "1.0 = normal"},
                    "remove_silence": {"type": "bool", "label": "Stille entfernen", "default": False},
                    "nfe_steps": {"type": "int", "label": "NFE Steps", "default": 32, "min": 1, "max": 64, "description": "Mehr = bessere Qualitaet, langsamer"},
                    "custom_cfg": {"type": "text", "label": "Custom Config (JSON)", "description": "Base-Architektur Config fuer alle Custom-Modelle"},
                },
            },
            "comfyui": {
                "label": "ComfyUI TTS (Qwen3-TTS)",
                "fields": {
                    "skill": {"type": "comfyui_backend_select", "label": "ComfyUI Backend(s)", "multi": True, "description": "URL und Queue werden automatisch vom Backend uebernommen"},
                    "mode": {
                        "type": "select",
                        "label": "Modus",
                        "choices": ["auto", "voiceclone", "voicedesc", "voicename"],
                        "default": "auto",
                        "description": "auto: voicedesc beim ersten Mal, danach voicename",
                    },
                    "workflow_voiceclone": {"type": "str", "label": "Workflow Voiceclone"},
                    "workflow_voicedesc": {"type": "str", "label": "Workflow Voicedesc"},
                    "workflow_voicename": {"type": "str", "label": "Workflow Voicename"},
                    "vram_required": {"type": "int", "label": "VRAM (GB)", "min": 0, "description": "Ueberschreibt Backend-Default"},
                    "max_wait": {"type": "int", "label": "Max Wait (s)", "default": 300},
                    "poll_interval": {"type": "float", "label": "Poll Interval (s)", "default": 1.0, "step": 0.5},
                },
            },
        },
    },
    "skills": {
        "label": "Skills",
        "icon": "🛠",
        "subsections": {
            "searx": {
                "label": "SearX Web Search",
                "fields": {
                    "enabled": {"type": "bool", "label": "Aktiviert", "default": False},
                    "url": {"type": "str", "label": "SearX URL"},
                    "engines": {"type": "str", "label": "Engines", "default": "google,duckduckgo,bing", "description": "Kommaseparierte Suchmaschinen"},
                    "categories": {"type": "str", "label": "Kategorien", "default": "general"},
                    "num_results": {"type": "int", "label": "Max Ergebnisse", "default": 5, "min": 1, "max": 50},
                },
            },
            "instagram": {
                "label": "Instagram",
                "fields": {
                    "enabled": {"type": "bool", "label": "Aktiviert", "default": False},
                    "caption_language": {"type": "select", "label": "Caption Sprache", "choices": ["de", "en", "fr", "es", "it"], "default": "en"},
                    "default_popularity": {"type": "int", "label": "Default Popularität", "default": 50, "min": 0, "max": 100, "description": "Default-Popularitaet fuer neue Characters (0-100%, per-Character ueberschreibbar)"},
                    "imagegen_default": {"type": "imagegen_select", "label": "Default ImageGen"},
                    "pending_window_hours": {"type": "int", "label": "Recent-Posts Window (h)", "default": 4, "min": 1, "max": 72, "description": "Wie lange neue Instagram-Posts als 'pending' im Agent-Thought-Prompt sichtbar sind (Stunden). Standardwert 4."},
                },
            },
            "set_location": {
                "label": "SetLocation",
                "fields": {
                    "enabled": {"type": "bool", "label": "Aktiviert", "default": True},
                },
            },
            "set_activity": {
                "label": "SetActivity",
                "fields": {
                    "enabled": {"type": "bool", "label": "Aktiviert", "default": True},
                },
            },
            "set_mood": {
                "label": "SetMood",
                "fields": {
                    "enabled": {"type": "bool", "label": "Aktiviert", "default": True},
                },
            },
            "talk_to": {
                "label": "TalkTo (face-to-face)",
                "fields": {
                    "enabled": {"type": "bool", "label": "Aktiviert", "default": True},
                },
            },
            "send_message": {
                "label": "SendMessage (remote)",
                "fields": {
                    "enabled": {"type": "bool", "label": "Aktiviert", "default": True},
                },
            },
            "outfit_change": {
                "label": "Outfit Change",
                "fields": {
                    "generate_image": {"type": "bool", "label": "Bild generieren", "default": True},
                    "language": {"type": "select", "label": "Sprache", "choices": ["de", "en"], "default": "en"},
                    "max_outfits": {"type": "int", "label": "Max Outfits", "default": 10, "min": 1, "max": 100, "description": "Maximale Anzahl gespeicherter Outfits pro Character (aelteste werden entfernt)"},
                    "cooldown_minutes": {"type": "int", "label": "Outfit-Cooldown (Min)", "default": 120, "min": 0, "max": 1440, "description": "Minuten bis ein LLM-gesteuerter Outfit-Wechsel am gleichen Ort moeglich ist. 0 = kein Cooldown. Gilt nicht bei Location-Wechsel oder User-Anfrage."},
                },
            },
            "markdown_writer": {
                "label": "Markdown Writer",
                "fields": {
                    "folders": {"type": "str", "label": "Ordner (kommasepariert)", "default": "diary,notes,guides"},
                    "default_folder": {"type": "str", "label": "Default Ordner", "default": "diary"},
                    "max_size_kb": {"type": "int", "label": "Max Größe (KB)", "default": 512, "min": 1},
                    "max_files": {"type": "int", "label": "Max Dateien", "default": 50, "min": 1},
                },
            },
        },
    },
    "knowledge": {
        "label": "Knowledge System",
        "icon": "📚",
        "fields": {
            "max_prompt_entries": {"type": "int", "label": "Max Prompt Entries", "default": 20, "min": 1, "description": "Max Eintraege im System-Prompt (Token-Budget)"},
            "max_entries": {"type": "int", "label": "Max Entries", "default": 200, "min": 1, "description": "Max gespeicherte Eintraege pro Character (Sliding Window)"},
            "daily_summary_days": {"type": "int", "label": "Daily Summary Tage", "default": 7, "min": 1, "description": "Anzahl vergangene Tage im System-Prompt"},
            "batch_size": {"type": "int", "label": "Batch Size", "default": 5, "min": 1},
            "max_input_tokens": {"type": "int", "label": "Max Input Tokens", "default": 12000, "min": 100},
            "max_output_tokens": {"type": "int", "label": "Max Output Tokens", "default": 1500, "min": 100},
            "search_max_candidates": {"type": "int", "label": "Search Max Candidates", "default": 50, "min": 1},
            "search_max_return": {"type": "int", "label": "Search Max Return", "default": 8, "min": 1},
        },
    },
    "relationships": {
        "label": "Relationships",
        "icon": "❤",
        "fields": {
            "summary_enabled": {"type": "bool", "label": "Summaries Aktiviert", "default": True, "description": "Periodische Zusammenfassung der Beziehungen"},
            "summary_interval_minutes": {"type": "int", "label": "Summary Interval (min)", "default": 120, "min": 10},
        },
    },
    "social_reactions": {
        "label": "Social Reactions",
        "icon": "👥",
        "fields": {
            "enabled": {"type": "bool", "label": "Aktiviert", "default": True, "description": "Wenn ein Character postet, reagieren andere Characters (Background-Queue)"},
        },
    },
    "thoughts": {
        "label": "Gedanken",
        "icon": "🧠",
        "fields": {
            "min_idle_minutes": {"type": "int", "label": "Min Idle (min)", "default": 5, "min": 1, "description": "Mindest-Idle-Zeit des Users bevor Gedanken-Ticks starten"},
            "min_scheduler_gap_minutes": {"type": "int", "label": "Min Scheduler Gap (min)", "default": 5, "min": 1, "description": "Mindestabstand zum naechsten Scheduler-Job"},
        },
    },
    "random_events": {
        "label": "Zufaellige Events",
        "icon": "🎲",
        "fields": {
            "enabled": {"type": "bool", "label": "Aktiviert", "default": True, "description": "Automatische Event-Generierung an besetzten Locations"},
            "base_probability": {"type": "int", "label": "Basis-Wahrscheinlichkeit %", "default": 5, "min": 0, "max": 50, "description": "Wahrscheinlichkeit pro Stunde pro Location. Pro Location ueberschreibbar."},
            "resolution_proactive": {"type": "bool", "label": "Proaktive Event-Aufloesung", "default": True, "description": "Characters an betroffener Location versuchen offene disruption/danger Events automatisch zu loesen (alle 5 Min)."},
            "resolution_cooldown_minutes": {"type": "int", "label": "Resolution Cooldown (min)", "default": 15, "min": 1, "max": 240, "description": "Mindestabstand zwischen zwei Loesungsversuchen am gleichen Event."},
        },
    },
    "story_engine": {
        "label": "Story Engine",
        "icon": "📖",
        "fields": {
            "enabled": {"type": "bool", "label": "Aktiviert", "default": False, "description": "Story Arc Fortschritt (Background-Prozess)"},
            "max_active_arcs": {"type": "int", "label": "Max Active Arcs", "default": 2, "min": 1, "description": "Maximale aktive Arcs pro User"},
            "cooldown_hours": {"type": "int", "label": "Cooldown (Stunden)", "default": 6, "min": 1, "description": "Mindest-Cooldown zwischen Arc-Advances pro User"},
            "max_beats": {"type": "int", "label": "Max Beats", "default": 5, "min": 1, "description": "Maximale Beats pro Arc bevor Aufloesung"},
            "beat_images": {"type": "bool", "label": "Beat Bilder", "default": True, "description": "Bilder pro Story-Beat generieren"},
            "imagegen_default": {"type": "imagegen_select", "label": "Default ImageGen"},
            "beat_faceswap": {"type": "bool", "label": "Beat FaceSwap", "default": False, "description": "FaceSwap bei Story-Arc Beat-Bildern anwenden"},
        },
    },
    "inventory": {
        "label": "Inventar / Items",
        "icon": "📦",
        "fields": {
            "item_image_width": {
                "type": "int",
                "label": "Item-Bild Breite (px)",
                "default": 256,
                "min": 64,
                "max": 2048,
                "description": "Aufloesung fuer generierte Item-Bilder (Icons im Inventar)",
            },
            "item_image_height": {
                "type": "int",
                "label": "Item-Bild Hoehe (px)",
                "default": 256,
                "min": 64,
                "max": 2048,
                "description": "Aufloesung fuer generierte Item-Bilder (Icons im Inventar)",
            },
        },
    },
    "ui": {
        "label": "UI / Themes",
        "icon": "🎨",
        "fields": {
            "default_theme": {
                "type": "select",
                "label": "Default Theme",
                "choices": ["default", "minimal", "dark"],
            },
            "available_themes": {"type": "str", "label": "Verfügbare Themes", "default": "default,minimal,dark"},
        },
    },
    "messaging_frame": {
        "label": "Messaging-Frame (Phone-Chat-Layout)",
        "icon": "📱",
        "fields": {
            "prompt": {
                "type": "text",
                "label": "Bild-Prompt",
                "default": "photorealistic modern smartphone, isolated on white, screen is pure chroma green, centered, no person, no reflection, top-down product photo",
                "description": "Beschreibung des Frames. Wichtig: 'pure green screen' / 'chroma green' fuer die Anzeigeflaeche — sonst kann der Chroma-Key sie nicht erkennen. Beispiel Fantasy: 'ornate magical mirror, gold frame, mirror surface pure green, no reflection'",
            },
            "target": {
                "type": "imagegen_target_select",
                "label": "Workflow / Backend",
                "description": "Welche Image-Pipeline soll das Frame rendern? ComfyUI-Workflows nutzen ihre konfigurierten Models/LoRAs/Switches automatisch. Cloud-Backends (Together, CivitAI) verwenden ihren konfigurierten Modellnamen. Offline-Optionen sind ausgegraut.",
            },
            "_grp_actions": {"type": "group_header", "label": "Aktion"},
            "_action_generate": {
                "type": "button",
                "label": "Frame generieren",
                "endpoint": "/world/messaging-frame/generate",
                "method": "POST",
                "body_from": ["prompt", "target"],
                "preview_url": "/world/messaging-frame.png",
                "description": "Generiert das Frame-Bild via konfiguriertem Image-Backend (~30-90s). Das alte Bild wird ueberschrieben. Im Fehlerfall (Green-Region nicht erkennbar) Prompt anpassen und erneut versuchen.",
            },
            "_preview": {
                "type": "image_preview",
                "label": "Aktuelles Frame",
                "url": "/world/messaging-frame.png",
                "meta_url": "/world/messaging-frame",
                "description": "Aktuell gespeichertes Frame (transparente Anzeigeflaeche im Browser ggf. als Schachbrett sichtbar).",
            },
            "_action_delete": {
                "type": "button",
                "label": "Frame entfernen",
                "endpoint": "/world/messaging-frame",
                "method": "DELETE",
                "confirm": "Frame wirklich entfernen? Das Phone-Chat-Layout faellt dann auf den Default-CSS-Frame zurueck.",
            },
        },
    },
}


def get_schema() -> dict:
    """Return the full schema for the admin API."""
    return SECTIONS
