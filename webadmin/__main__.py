from .app import create_app
from .settings import Settings

if __name__ == "__main__":
    settings = Settings.from_env()
    app = create_app(settings)
    try:
        from waitress import serve
    except ImportError:  # dev fallback only
        app.run(host=settings.host, port=settings.port)
    else:
        serve(app, host=settings.host, port=settings.port, threads=4)
