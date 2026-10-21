
"""Main entry point for EVA."""

from eva.core.orchestrator import EVAOrchestrator


def show_banner():
    print()
    print("=" * 44)
    print("       EVA - Evolving Virtual Assistant")
    print("=" * 44)
    print("  Local AI | Conversation Memory | CLI")
    print("  Type /help to see available commands.")
    print("=" * 44)
    print()


def show_help():
    print("""
EVA COMMANDS

  /help    Show available commands
  /status  Show current session information
  /automation  Check desktop automation capabilities
  /reset   Clear conversation memory
  /exit    Close EVA

Anything else you type will be sent to EVA.
""")


def main():
    show_banner()

    try:
        eva = EVAOrchestrator()
    except Exception as error:
        print(f"[Startup error] {error}")
        return

    print("EVA: I'm ready. What's on your mind?\n")

    while True:
        try:
            user_input = input("You: ").strip()

            if not user_input:
                continue

            command = user_input.lower()

            if command == "/help":
                show_help()
                continue

            if command == "/status":
                status = eva.get_status()
                print("\n--- EVA STATUS ---")
                print(f"Model: {status['model']}")
                print(
                    "Stored messages: "
                    f"{status['stored_messages']}"
                )
                print(
                    "Online intelligence: "
                    f"{'Enabled' if status['online_enabled'] else 'Disabled'}"
                )
                print("------------------\n")
                continue

            if command == "/automation":
                print(f"\\nEVA: {eva.respond('automation status')}\\n")
                continue

            if command == "/reset":
                eva.reset_conversation()
                print("EVA: Fresh start. Our conversation is cleared.\n")
                continue

            if command in ("/exit", "/quit"):
                print("EVA: See you later! 👋")
                break

            reply = eva.respond(user_input)
            print(f"\nEVA: {reply}\n")

        except (KeyboardInterrupt, EOFError):
            print("\nEVA: Shutting down. See you soon! 👋")
            break

        except Exception as error:
            print(f"\n[Error] Something went wrong: {error}\n")


if __name__ == "__main__":
    main()
