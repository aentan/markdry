import os
import sys
import threading
import time
import webbrowser


def _fix_ssl() -> None:
    try:
        import certifi
        os.environ.setdefault("SSL_CERT_FILE", certifi.where())
        os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
    except ImportError:
        pass


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else "gui"

    if cmd == "gui":
        _fix_ssl()
        from markdry.desktop import run
        run()

    elif cmd == "serve":
        _fix_ssl()

        port = 7777
        url = f"http://localhost:{port}"

        def _open_browser():
            time.sleep(1.2)
            webbrowser.open(url)

        threading.Thread(target=_open_browser, daemon=True).start()

        import logging
        logging.basicConfig(
            level=logging.INFO,
            format="%(levelname)s [%(name)s] %(message)s",
        )

        import uvicorn
        uvicorn.run(
            "markdry.server:app",
            host="127.0.0.1",
            port=port,
            reload=False,
            log_level="info",
        )

    else:
        print("Usage: markdry [gui|serve]")
        sys.exit(1)


if __name__ == "__main__":
    main()
