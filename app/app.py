from flask import Flask, render_template, request, session
from flask_babel import Babel
from app.routes.landing import landing_bp
from app.routes.bills import bills_bp
from app.routes.congress import congress_bp
from app.routes.i18n import i18n_bp

# from app.routes import landing_bp, bills_bp, congress_bp, info_bp
# For API
from flask_smorest import Api
from backend.config import settings
from app.routes.api import register_api


babel = Babel()


def get_locale():
    lang = session.get("lang")
    if not lang:
        lang = request.args.get("lang")
    if lang:
        return lang
    return "es"


def create_app():
    app = Flask(__name__)
    app.json.sort_keys = False

    app.register_blueprint(landing_bp)
    app.register_blueprint(bills_bp)
    app.register_blueprint(congress_bp)
    app.register_blueprint(i18n_bp)

    app.secret_key = "secret"

    app.config["BABEL_DEFAULT_LOCALE"] = "es"
    # For initializing the API
    app.config["API_TITLE"] = settings.API_TITLE
    app.config["API_VERSION"] = settings.API_VERSION
    app.config["OPENAPI_VERSION"] = settings.OPENAPI_VERSION
    app.config["OPENAPI_URL_PREFIX"] = settings.OPENAPI_URL_PREFIX
    app.config["OPENAPI_SWAGGER_UI_PATH"] = settings.OPENAPI_SWAGGER_UI_PATH
    app.config["OPENAPI_SWAGGER_UI_URL"] = settings.OPENAPI_SWAGGER_UI_URL
    app.config["API_SPEC_OPTIONS"] = {
        "components": {
            "securitySchemes": {
                "bearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "bearerFormat": "API key",
                }
            }
        },
        "security": [{"bearerAuth": []}],
    }

    babel.init_app(app, locale_selector=get_locale)

    @app.errorhandler(404)
    def not_found(error):
        return render_template("errors/404.html"), 404

    api = Api(app)
    register_api(api)
    return app


if __name__ == "__main__":
    app = create_app()
    app.run(debug=True)
