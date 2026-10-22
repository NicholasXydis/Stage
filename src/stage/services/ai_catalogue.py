from stage.domain.ai_apps import (
    AiApp,
    ConfigFormat,
    Editor,
    LinkStyle,
    OwnCommand,
    SettingsFile,
    Shape,
    Where,
)

JSON = ConfigFormat.JSON
TOML = ConfigFormat.TOML
YAML = ConfigFormat.YAML
SERVERS = ("mcpServers",)


def _bundle(name: str) -> tuple[Where, ...]:
    return (
        Where(macos=f"/Applications/{name}.app"),
        Where(macos=f"~/Applications/{name}.app"),
    )


def _electron(folder: str) -> Where:
    return Where(f"{{support}}/{folder}", f"{{appdata}}/{folder}", f"{{config}}/{folder}")


def _file(
    where: Where | tuple[Where, ...],
    kind: ConfigFormat = JSON,
    key: tuple[str, ...] = SERVERS,
    shape: Shape = Shape.PLAIN,
    *,
    locked: bool = False,
) -> SettingsFile:
    places = where if isinstance(where, tuple) else (where,)
    return SettingsFile(places, kind, key, shape, locked)


VSCODE_PACKAGE = (
    Where(macos="/Applications/Visual Studio Code.app/Contents/Resources/app/package.json"),
    Where(macos="~/Applications/Visual Studio Code.app/Contents/Resources/app/package.json"),
    Where(windows="{localappdata}/Programs/Microsoft VS Code/resources/app/package.json"),
    Where(linux="/usr/share/code/resources/app/package.json"),
    Where(linux="/snap/code/current/usr/share/code/resources/app/package.json"),
)
VSCODE_FOUND = (
    *_bundle("Visual Studio Code"),
    Where(windows="{localappdata}/Programs/Microsoft VS Code"),
    Where(linux="/usr/share/code"),
    Where(linux="/snap/code/current"),
)
COPILOT_GLOBAL_SINCE = "1.140"

EDITORS: tuple[Editor, ...] = (
    Editor("vscode", "VS Code", _electron("Code/User"), "~/.vscode/extensions"),
    Editor(
        "vscode-insiders",
        "VS Code Insiders",
        _electron("Code - Insiders/User"),
        "~/.vscode-insiders/extensions",
    ),
    Editor("vscodium", "VSCodium", _electron("VSCodium/User"), "~/.vscode-oss/extensions"),
    Editor("cursor", "Cursor", _electron("Cursor/User"), "~/.cursor/extensions"),
    Editor("devin", "Devin Desktop", _electron("Devin/User"), "~/.devin/extensions"),
    Editor("windsurf", "Windsurf", _electron("Windsurf/User"), "~/.windsurf/extensions"),
    Editor("kiro", "Kiro", _electron("Kiro/User"), "~/.kiro/extensions"),
    Editor("trae", "Trae", _electron("Trae/User"), "~/.trae/extensions"),
)

APPS: tuple[AiApp, ...] = (
    AiApp(
        "claude-code",
        "Claude Code",
        file=_file(Where.everywhere("{env:CLAUDE_CONFIG_DIR|~}/.claude.json"), shape=Shape.STDIO),
        command=OwnCommand(
            "claude",
            ("mcp", "add-json", "--scope", "user", "{name}", "{entry}"),
            ("mcp", "remove", "--scope", "user", "{name}"),
        ),
        programs=("claude",),
        markers=(Where.everywhere("~/.claude"),),
        bundled=(
            "anthropic.claude-code-*/resources/native-binary/claude",
            "anthropic.claude-code-*/resources/native-binary/claude.exe",
        ),
        edits_file=False,
        note="Applies to conversations started afterwards.",
        docs="https://code.claude.com/docs/en/mcp",
    ),
    AiApp(
        "codex",
        "Codex",
        file=_file(
            Where.everywhere("{env:CODEX_HOME|~/.codex}/config.toml"), TOML, ("mcp_servers",)
        ),
        command=OwnCommand(
            "codex",
            ("mcp", "add", "{name}", "{env}", "--", "{command}", "{args}"),
            ("mcp", "remove", "{name}"),
            "--env",
        ),
        programs=("codex",),
        markers=(Where.everywhere("{env:CODEX_HOME|~/.codex}"), *_bundle("Codex")),
        note="Applies to sessions started afterwards.",
        docs="https://learn.chatgpt.com/docs/extend/mcp",
    ),
    AiApp(
        "chatgpt",
        "ChatGPT desktop",
        file=_file(
            Where.everywhere("{env:CODEX_HOME|~/.codex}/config.toml"), TOML, ("mcp_servers",)
        ),
        markers=_bundle("ChatGPT"),
        note="Local servers work in the Codex view, not in regular chats.",
        docs="https://learn.chatgpt.com/docs/extend/mcp",
    ),
    AiApp(
        "copilot",
        "VS Code and Copilot CLI",
        file=_file(
            Where.everywhere("{env:COPILOT_HOME|~/.copilot}/mcp-config.json"), shape=Shape.COPILOT
        ),
        command=OwnCommand(
            "copilot",
            ("mcp", "add", "{name}", "{env}", "--tools", "*", "--", "{command}", "{args}"),
            ("mcp", "remove", "{name}"),
            "--env",
        ),
        programs=("copilot",),
        markers=(Where.everywhere("{env:COPILOT_HOME|~/.copilot}"), *VSCODE_FOUND),
        note="Read by VS Code 1.140 and later and by the Copilot CLI.",
        docs="https://code.visualstudio.com/docs/copilot/customization/mcp-servers",
    ),
    AiApp(
        "vscode",
        "VS Code",
        file=_file(_electron("Code/User/mcp.json"), key=("servers",), shape=Shape.STDIO),
        command=OwnCommand("code", ("--add-mcp", "{json}")),
        programs=("code",),
        markers=VSCODE_FOUND,
        version_files=VSCODE_PACKAGE,
        since="0",
        before=COPILOT_GLOBAL_SINCE,
        docs="https://code.visualstudio.com/docs/copilot/reference/mcp-configuration",
    ),
    AiApp(
        "gemini",
        "Gemini CLI",
        file=_file(Where.everywhere("{env:GEMINI_CLI_HOME|~}/.gemini/settings.json")),
        command=OwnCommand(
            "gemini",
            ("mcp", "add", "-s", "user", "{name}", "{env}", "{command}", "{args}"),
            ("mcp", "remove", "-s", "user", "{name}"),
            "-e",
        ),
        programs=("gemini",),
        markers=(Where.everywhere("{env:GEMINI_CLI_HOME|~}/.gemini"),),
        note="Gemini starts it only in folders you have trusted. Gemini Code Assist in VS "
        "Code reads the same settings.",
        docs="https://github.com/google-gemini/gemini-cli/blob/main/docs/tools/mcp-server.md",
    ),
    AiApp(
        "qwen",
        "Qwen Code",
        file=_file(Where.everywhere("{env:QWEN_HOME|~/.qwen}/settings.json")),
        command=OwnCommand(
            "qwen",
            ("mcp", "add", "{name}", "{env}", "{command}", "{args}"),
            ("mcp", "remove", "{name}"),
            "-e",
        ),
        programs=("qwen",),
        markers=(Where.everywhere("{env:QWEN_HOME|~/.qwen}"),),
        docs="https://qwenlm.github.io/qwen-code-docs/",
    ),
    AiApp(
        "opencode",
        "OpenCode",
        file=_file(
            (
                Where(
                    "{config}/opencode/opencode.json",
                    "~/.config/opencode/opencode.json",
                    "{config}/opencode/opencode.json",
                ),
                Where(
                    "{config}/opencode/opencode.jsonc",
                    "~/.config/opencode/opencode.jsonc",
                    "{config}/opencode/opencode.jsonc",
                ),
            ),
            key=("mcp",),
            shape=Shape.OPENCODE,
        ),
        programs=("opencode",),
        markers=(Where("{config}/opencode", "~/.config/opencode", "{config}/opencode"),),
        restart=True,
        note="OpenCode 2 reads this entry too.",
        docs="https://opencode.ai/docs/mcp-servers",
    ),
    AiApp(
        "kilo",
        "Kilo Code",
        file=_file(
            tuple(
                Where(
                    f"{{config}}/kilo/{name}", f"~/.config/kilo/{name}", f"{{config}}/kilo/{name}"
                )
                for name in (
                    "kilo.jsonc",
                    "config.json",
                    "kilo.json",
                    "opencode.json",
                    "opencode.jsonc",
                )
            ),
            key=("mcp",),
            shape=Shape.OPENCODE,
        ),
        command=OwnCommand(
            "kilo",
            ("mcp", "add", "{name}", "{env}", "--", "{command}", "{args}"),
            env_flag="--env",
        ),
        programs=("kilo",),
        markers=(Where("{config}/kilo", "~/.config/kilo", "{config}/kilo"),),
        extension="kilocode.kilo-code",
        since="7.0.0",
        docs="https://kilo.ai/docs",
    ),
    AiApp(
        "kilo-legacy",
        "Kilo Code",
        file=_file(
            Where.everywhere("{user}/globalStorage/kilocode.kilo-code/settings/mcp_settings.json"),
            shape=Shape.NOT_DISABLED,
        ),
        extension="kilocode.kilo-code",
        since="0",
        before="7.0.0",
    ),
    AiApp(
        "hermes",
        "Hermes Agent",
        file=_file(Where.everywhere("{hermes}/config.yaml"), YAML, ("mcp_servers",), Shape.ENABLED),
        programs=("hermes",),
        markers=(Where.everywhere("{hermes}"),),
        restart=True,
        note="Hermes starts servers only with its MCP support installed: "
        "pip install 'hermes-agent[mcp]', or its own installer.",
        docs="https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp",
    ),
    AiApp(
        "goose",
        "Goose",
        file=_file(
            Where(
                "{config}/goose/config.yaml",
                "{appdata}/Block/goose/config/config.yaml",
                "{config}/goose/config.yaml",
            ),
            YAML,
            ("extensions",),
            Shape.GOOSE,
        ),
        programs=("goose",),
        markers=(
            Where("{config}/goose", "{appdata}/Block/goose", "{config}/goose"),
            *_bundle("Goose"),
        ),
        note="Applies to sessions started afterwards.",
        docs="https://goose-docs.ai/docs/guides/config-files",
    ),
    AiApp(
        "crush",
        "Crush",
        file=_file(
            Where(
                "{config}/crush/crush.json",
                "~/.config/crush/crush.json",
                "{config}/crush/crush.json",
            ),
            key=("mcp",),
            shape=Shape.STDIO,
        ),
        programs=("crush",),
        markers=(Where("{config}/crush", "~/.config/crush", "{config}/crush"),),
        docs="https://github.com/charmbracelet/crush",
    ),
    AiApp(
        "amp",
        "Amp",
        file=_file(
            (
                Where.everywhere("{env:AMP_SETTINGS_FILE|{config}/amp/settings.json}"),
                Where.everywhere("{config}/amp/settings.jsonc"),
            ),
            key=("amp.mcpServers",),
        ),
        command=OwnCommand(
            "amp",
            ("mcp", "add", "{name}", "{env}", "--", "{command}", "{args}"),
            ("mcp", "remove", "{name}"),
            "--env",
        ),
        programs=("amp",),
        markers=(Where.everywhere("{config}/amp"),),
        docs="https://ampcode.com/manual",
    ),
    AiApp(
        "droid",
        "Factory Droid",
        file=_file(Where.everywhere("~/.factory/mcp.json"), shape=Shape.STDIO_NOT_DISABLED),
        programs=("droid",),
        markers=(Where.everywhere("~/.factory"),),
        docs="https://docs.factory.ai/cli/configuration/mcp",
    ),
    AiApp(
        "auggie",
        "Augment Auggie",
        file=_file(Where.everywhere("~/.augment/settings.json"), shape=Shape.STDIO),
        command=OwnCommand(
            "auggie",
            ("mcp", "add-json", "{name}", "{entry}", "--replace"),
            ("mcp", "remove", "{name}"),
        ),
        programs=("auggie",),
        markers=(Where.everywhere("~/.augment"),),
        docs="https://docs.augmentcode.com/cli/integrations",
    ),
    AiApp(
        "kimi",
        "Kimi Code",
        file=_file(Where.everywhere("{env:KIMI_CODE_HOME|~/.kimi-code}/mcp.json")),
        programs=("kimi",),
        markers=(Where.everywhere("{env:KIMI_CODE_HOME|~/.kimi-code}"),),
        docs="https://www.kimi.com/code",
    ),
    AiApp(
        "openhands",
        "OpenHands",
        file=_file(Where.everywhere("~/.openhands/mcp.json")),
        programs=("openhands",),
        markers=(Where.everywhere("~/.openhands"),),
        docs="https://docs.openhands.dev",
    ),
    AiApp(
        "grok",
        "Grok Build",
        file=_file(Where.everywhere("~/.grok/config.toml"), TOML, ("mcp_servers",)),
        programs=("grok",),
        markers=(Where.everywhere("~/.grok"),),
        note="Grok also loads Claude Code's servers, so Stage may show twice there.",
    ),
    AiApp(
        "codebuddy",
        "CodeBuddy Code",
        file=_file(Where.everywhere("~/.codebuddy/.mcp.json")),
        command=OwnCommand(
            "codebuddy",
            ("mcp", "add", "--scope", "user", "{name}", "{env}", "--", "{command}", "{args}"),
            ("mcp", "remove", "--scope", "user", "{name}"),
            "-e",
        ),
        programs=("codebuddy",),
        markers=(Where.everywhere("~/.codebuddy"),),
    ),
    AiApp(
        "vibe",
        "Mistral Vibe",
        file=_file(Where.everywhere("{env:VIBE_HOME|~/.vibe}/config.toml"), TOML, ("mcp_servers",)),
        edits_file=False,
        command=OwnCommand(
            "vibe",
            (
                "mcp",
                "add",
                "{name}",
                "--transport",
                "stdio",
                "--command",
                "{command}",
                "{each-arg}",
                "{env}",
            ),
            ("mcp", "remove", "{name}"),
            "--env",
        ),
        programs=("vibe",),
        markers=(Where.everywhere("{env:VIBE_HOME|~/.vibe}"),),
        screens="Or add this to ~/.vibe/config.toml:\n"
        '[[mcp_servers]]\nname = "{name}"\ntransport = "stdio"\ncommand = {command}\nargs = {args}',
        docs="https://docs.mistral.ai/vibe",
    ),
    AiApp(
        "continue-cli",
        "Continue CLI",
        file=_file(
            Where.everywhere("{env:CONTINUE_GLOBAL_DIR|~/.continue}/config.yaml"), YAML, SERVERS
        ),
        edits_file=False,
        programs=("cn",),
        screens="Add this under mcpServers in ~/.continue/config.yaml:\n"
        "  - name: {name}\n    type: stdio\n    command: {command}\n    args: {args}",
    ),
    AiApp(
        "claude-desktop",
        "Claude Desktop",
        file=_file(
            (
                Where(
                    "{support}/Claude/claude_desktop_config.json",
                    "{appdata}/Claude/claude_desktop_config.json",
                    "{config}/Claude/claude_desktop_config.json",
                ),
                Where(
                    windows="{localappdata}/Packages/Claude_pzs8sxrjxfjjc/LocalCache/Roaming"
                    "/Claude/claude_desktop_config.json"
                ),
            )
        ),
        markers=(
            *_bundle("Claude"),
            _electron("Claude"),
            Where(windows="{localappdata}/AnthropicClaude"),
        ),
        process="Claude",
        closed_first=True,
        restart=True,
        docs="https://modelcontextprotocol.io/docs/develop/connect-local-servers",
    ),
    AiApp(
        "cursor",
        "Cursor",
        file=_file(Where.everywhere("~/.cursor/mcp.json"), shape=Shape.STDIO),
        programs=("cursor-agent",),
        markers=(
            *_bundle("Cursor"),
            Where.everywhere("~/.cursor"),
            Where(windows="{localappdata}/Programs/cursor"),
        ),
        restart=True,
        docs="https://cursor.com/docs/context/mcp",
    ),
    AiApp(
        "devin",
        "Devin Desktop",
        file=_file(
            Where(
                "{config}/devin/mcp_config.json",
                "{appdata}/devin/mcp_config.json",
                "{config}/devin/mcp_config.json",
            )
        ),
        command=OwnCommand(
            "devin",
            ("mcp", "add", "{name}", "-s", "user", "{env}", "--", "{command}", "{args}"),
            ("mcp", "remove", "{name}", "-s", "user"),
            "-e",
        ),
        programs=("devin",),
        markers=(*_bundle("Devin"), Where("{config}/devin", "{appdata}/devin", "{config}/devin")),
        note="Devin also reads other apps' servers, so Stage may show twice there.",
        docs="https://docs.devin.ai/desktop/cascade/mcp",
    ),
    AiApp(
        "windsurf",
        "Windsurf",
        file=_file(Where.everywhere("~/.codeium/windsurf/mcp_config.json")),
        markers=(*_bundle("Windsurf"), Where.everywhere("~/.codeium/windsurf")),
        restart=True,
        docs="https://docs.devin.ai/windsurf/plugins/cascade/mcp",
    ),
    AiApp(
        "zed",
        "Zed",
        file=_file(
            Where(
                "~/.config/zed/settings.json",
                "{appdata}/Zed/settings.json",
                "{config}/zed/settings.json",
            ),
            key=("context_servers",),
        ),
        programs=("zed",),
        markers=(*_bundle("Zed"), Where("~/.config/zed", "{appdata}/Zed", "{config}/zed")),
        docs="https://zed.dev/docs/ai/mcp",
    ),
    AiApp(
        "kiro",
        "Kiro",
        file=_file(
            Where.everywhere("{env:KIRO_HOME|~/.kiro}/settings/mcp.json"), shape=Shape.NOT_DISABLED
        ),
        programs=("kiro-cli", "kiro"),
        markers=(*_bundle("Kiro"), Where.everywhere("{env:KIRO_HOME|~/.kiro}")),
        docs="https://kiro.dev/docs/mcp/configuration/",
    ),
    AiApp(
        "trae",
        "Trae",
        file=_file(_electron("Trae/User/mcp.json")),
        markers=(*_bundle("Trae"), Where.everywhere("~/.trae")),
        spaceless=True,
        docs="https://docs.trae.ai/ide/add-mcp-servers",
    ),
    AiApp(
        "warp",
        "Warp",
        file=_file(Where.everywhere("~/.warp/.mcp.json")),
        markers=(*_bundle("Warp"), Where.everywhere("~/.warp")),
        note="Warp can also start Claude Code's and Codex's servers, so Stage may show twice.",
        docs="https://docs.warp.dev/agents/capabilities/mcp",
    ),
    AiApp(
        "antigravity",
        "Antigravity",
        file=_file(Where.everywhere("~/.gemini/config/mcp_config.json")),
        markers=(*_bundle("Antigravity"), Where.everywhere("~/.gemini/config")),
        docs="https://antigravity.google/docs/mcp",
    ),
    AiApp(
        "visual-studio",
        "Visual Studio",
        file=_file(Where(windows="~/.mcp.json"), key=("servers",), shape=Shape.STDIO),
        markers=(
            Where(windows="C:/Program Files/Microsoft Visual Studio"),
            Where(windows="C:/Program Files (x86)/Microsoft Visual Studio"),
        ),
        docs="https://learn.microsoft.com/visualstudio/ide/mcp-servers",
    ),
    AiApp(
        "jetbrains",
        "JetBrains AI Assistant",
        markers=(_electron("JetBrains"),),
        screens="In your JetBrains IDE open Settings | Tools | AI Assistant | Model Context "
        "Protocol (MCP), choose Add, then paste:\n{json}",
        docs="https://www.jetbrains.com/help/ai-assistant/mcp.html",
    ),
    AiApp(
        "junie",
        "Junie",
        file=_file(Where.everywhere("~/.junie/mcp/mcp.json")),
        programs=("junie",),
        markers=(Where.everywhere("~/.junie"),),
        docs="https://junie.jetbrains.com/docs/junie-cli-mcp-configuration.html",
    ),
    AiApp(
        "lm-studio",
        "LM Studio",
        file=_file(Where.everywhere("{lmstudio}/mcp.json")),
        programs=("lms",),
        markers=(
            *_bundle("LM Studio"),
            Where.everywhere("~/.lmstudio"),
            Where.everywhere("~/.cache/lm-studio"),
        ),
        docs="https://lmstudio.ai/docs/app/mcp",
    ),
    AiApp(
        "cline",
        "Cline",
        file=_file(
            Where.everywhere("{env:CLINE_DIR|~/.cline}/data/settings/cline_mcp_settings.json"),
            shape=Shape.NOT_DISABLED,
            locked=True,
        ),
        programs=("cline",),
        markers=(Where.everywhere("{env:CLINE_DIR|~/.cline}/data"),),
        extension="saoudrizwan.claude-dev",
        since="4.0.0",
        docs="https://docs.cline.bot/mcp/configuring-mcp-servers",
    ),
    AiApp(
        "cline-legacy",
        "Cline",
        file=_file(
            Where.everywhere(
                "{user}/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json"
            ),
            shape=Shape.NOT_DISABLED,
        ),
        extension="saoudrizwan.claude-dev",
        since="0",
        before="4.1.25",
    ),
    AiApp(
        "roo",
        "Roo Code",
        file=_file(
            Where.everywhere(
                "{user}/globalStorage/rooveterinaryinc.roo-cline/settings/mcp_settings.json"
            ),
            shape=Shape.NOT_DISABLED,
        ),
        extension="rooveterinaryinc.roo-cline",
        docs="https://docs.roocode.com/features/mcp/using-mcp-in-roo",
    ),
    AiApp(
        "continue",
        "Continue",
        file=_file(Where.everywhere("{env:CONTINUE_GLOBAL_DIR|~/.continue}/mcpServers/stage.json")),
        extension="continue.continue",
        markers=(Where.everywhere("{env:CONTINUE_GLOBAL_DIR|~/.continue}"),),
        docs="https://docs.continue.dev/customize/deep-dives/mcp",
    ),
    AiApp(
        "amazon-q",
        "Amazon Q",
        file=_file(Where.everywhere("~/.aws/amazonq/mcp.json"), shape=Shape.NOT_DISABLED),
        extension="amazonwebservices.amazon-q-vscode",
        markers=(Where.everywhere("~/.aws/amazonq"),),
        docs="https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/mcp-ide.html",
    ),
    AiApp(
        "augment",
        "Augment",
        extension="augment.vscode-augment",
        screens="In Augment's settings panel, add an MCP server with these fields:",
    ),
    AiApp(
        "anythingllm",
        "AnythingLLM",
        file=_file(_electron("anythingllm-desktop/storage/plugins/anythingllm_mcp_servers.json")),
        markers=(*_bundle("AnythingLLM"), _electron("anythingllm-desktop")),
        note="Press Refresh under Agent Skills to load Stage.",
        docs="https://docs.anythingllm.com/mcp-compatibility/desktop",
    ),
    AiApp(
        "jan",
        "Jan",
        file=_file(
            Where(
                "{env:JAN_DATA_ROOT|{support}/Jan/data}/mcp_config.json",
                "{env:JAN_DATA_ROOT|{appdata}/Jan/data}/mcp_config.json",
                "{env:JAN_DATA_ROOT|{data}/Jan/data}/mcp_config.json",
            ),
            shape=Shape.JAN,
        ),
        markers=(*_bundle("Jan"), Where("{support}/Jan", "{appdata}/Jan", "{data}/Jan")),
        process="Jan",
        closed_first=True,
        docs="https://docs.jan.ai/desktop/mcp",
    ),
    AiApp(
        "5ire",
        "5ire",
        file=_file(_electron("5ire/mcp.json"), shape=Shape.FIVE),
        markers=(*_bundle("5ire"), _electron("5ire")),
        docs="https://github.com/nanbingxyz/5ire",
    ),
    AiApp(
        "witsy",
        "Witsy",
        file=_file(_electron("Witsy/settings.json")),
        markers=(*_bundle("Witsy"), _electron("Witsy")),
        docs="https://github.com/Kochava-Studios/witsy",
    ),
    AiApp(
        "dive",
        "Dive",
        file=_file(Where.everywhere("~/.dive/config/mcp_config.json"), shape=Shape.DIVE),
        markers=(*_bundle("Dive"), Where.everywhere("~/.dive")),
        restart=True,
        docs="https://github.com/OpenAgentPlatform/Dive",
    ),
    AiApp(
        "chatbox",
        "Chatbox",
        link=LinkStyle.CHATBOX,
        markers=(*_bundle("Chatbox"), _electron("xyz.chatboxapp.app")),
        docs="https://docs.chatboxai.app/en/guides/mcp",
    ),
    AiApp(
        "cherry-studio",
        "Cherry Studio",
        link=LinkStyle.CHERRY,
        markers=(*_bundle("Cherry Studio"), _electron("CherryStudio")),
        note="Cherry Studio adds it switched off; switch it on and add it to an agent.",
        docs="https://docs.cherry-ai.com",
    ),
    AiApp(
        "deepchat",
        "DeepChat",
        link=LinkStyle.DEEPCHAT,
        markers=(*_bundle("DeepChat"), _electron("DeepChat")),
        note="DeepChat adds it switched off; switch it on in its MCP settings.",
        docs="https://github.com/ThinkInAIXYZ/deepchat",
    ),
    AiApp(
        "msty",
        "Msty Studio",
        markers=(*_bundle("Msty Studio"),),
        screens="In Msty Studio open Toolbox, choose Add New Tool, then STDIO/JSON, paste the "
        "entry below, and add the tool to a Toolset you use:\n{entry}",
    ),
    AiApp(
        "raycast",
        "Raycast",
        markers=_bundle("Raycast"),
        screens="In Raycast open AI settings, then MCP Servers, and add a server with these "
        "fields (Raycast Pro):",
    ),
    AiApp(
        "boltai",
        "BoltAI",
        markers=(*_bundle("BoltAI"), Where.everywhere("~/.boltai")),
        screens="In BoltAI open Settings, then MCP Servers. If Stage is already connected to "
        "Claude Desktop, use Import from Claude Desktop; otherwise add a server with these "
        "fields:",
    ),
    AiApp(
        "perplexity",
        "Perplexity",
        markers=_bundle("Perplexity"),
        screens="In the Perplexity Mac app open Settings, then Connectors, and add a custom "
        "local server with these fields (Perplexity Pro):",
    ),
    AiApp(
        "aionui",
        "AionUi",
        markers=_bundle("AionUi"),
        screens="In AionUi open MCP Management, choose Add via JSON and paste:\n{json}",
    ),
    AiApp(
        "chatwise",
        "ChatWise",
        markers=_bundle("ChatWise"),
        screens="Copy this, then in ChatWise open Settings, Tools, and import from clipboard "
        "(ChatWise Pro):\n{json}",
    ),
    AiApp(
        "lobehub",
        "LobeHub",
        markers=_bundle("LobeHub"),
        screens="In LobeHub desktop open Plugins, add a custom MCP plugin of type stdio with "
        "these fields:",
    ),
    AiApp(
        "librechat",
        "LibreChat",
        screens="On a LibreChat you run yourself, add this to librechat.yaml and restart it:\n"
        "mcpServers:\n  {name}:\n    type: stdio\n    command: {command}\n    args: {args}",
    ),
)
