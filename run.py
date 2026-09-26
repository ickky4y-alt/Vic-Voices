import os


def main():
    from pkg import create_app

    port = int(os.environ.get("PORT") or os.environ.get("VIC_VOICES_PORT", "5001"))
    host = os.environ.get("HOST", "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")
    create_app().run(debug=False, use_reloader=False, host=host, port=port)


if __name__ == "__main__":
    main()

