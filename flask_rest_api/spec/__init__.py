"""API specification using Open API"""

import json

import flask
from flask import current_app
import apispec

from .plugins import FlaskPlugin
from flask_rest_api.compat import APISPEC_VERSION_MAJOR
if APISPEC_VERSION_MAJOR == 0:
    from .plugins import MarshmallowPlugin
else:
    from apispec.ext.marshmallow import MarshmallowPlugin


class APISpecMixin:
    """Add APISpec related feature to Api class"""

    def _init_spec(
            self, *, flask_plugin=None, marshmallow_plugin=None,
            extra_plugins=None, **options
    ):
        # Plugins
        self.flask_plugin = flask_plugin or FlaskPlugin()
        self.ma_plugin = marshmallow_plugin or MarshmallowPlugin()
        plugins = [self.flask_plugin, self.ma_plugin]
        plugins.extend(extra_plugins or ())

        # APISpec options
        openapi_version = self._app.config.get('OPENAPI_VERSION', '2.0')
        openapi_major_version = int(openapi_version.split('.')[0])
        if openapi_major_version < 3:
            base_path = self._app.config.get('APPLICATION_ROOT')
            # Don't pass basePath if '/' to avoid a bug in apispec
            # https://github.com/marshmallow-code/apispec/issues/78#issuecomment-431854606
            # TODO: Remove this condition when the bug is fixed
            if base_path != '/':
                options.setdefault('basePath', base_path)
            options.setdefault('produces', ['application/json', ])
            options.setdefault('consumes', ['application/json', ])
        options.update(self._app.config.get('API_SPEC_OPTIONS', {}))

        # Instantiate spec
        self.spec = apispec.APISpec(
            self._app.name,
            self._app.config.get('API_VERSION', '1'),
            openapi_version=openapi_version,
            plugins=plugins,
            **options,
        )

        # Register custom fields in spec
        for args in self._fields:
            self._register_field(*args)
        # Register schema definitions in spec
        for name, schema_cls, kwargs in self._definitions:
            if APISPEC_VERSION_MAJOR < 1:
                self.spec.definition(name, schema=schema_cls, **kwargs)
            else:
                self.spec.components.schema(name, schema=schema_cls, **kwargs)
        # Register custom converters in spec
        for args in self._converters:
            self._register_converter(*args)

    def definition(self, name):
        """Decorator to register a Schema in the doc

        This allows a schema to be defined once in the `definitions`
        section of the spec and be referenced throughout the spec.

        :param str name: Name of the definition in the spec

            Example: ::

                @api.definition('Pet')
                class PetSchema(Schema):
                    ...
        """
        def decorator(schema_cls, **kwargs):
            self._definitions.append((name, schema_cls, kwargs))
            # Register definition in spec if app is already initialized
            if self.spec is not None:
                if APISPEC_VERSION_MAJOR < 1:
                    self.spec.definition(name, schema=schema_cls, **kwargs)
                else:
                    self.spec.components.schema(
                        name, schema=schema_cls, **kwargs)
            return schema_cls
        return decorator

    def register_converter(self, converter, conv_type, conv_format=None):
        """Register custom path parameter converter

        :param BaseConverter converter: Converter
            Subclass of werkzeug's BaseConverter
        :param str conv_type: Parameter type
        :param str conv_format: Parameter format (optional)

            Example: ::

                # Register converter in Flask app
                app.url_map.converters['uuid'] = UUIDConverter

                # Register converter in internal APISpec instance
                api.register_converter(UUIDConverter, 'string', 'UUID')

                @blp.route('/pets/{uuid:pet_id}')
                    ...

                api.register_blueprint(blp)

        Once the converter is registered, all paths using it will have
        corresponding path parameter documented with the right type and format.

        Should be called before registering paths with
        :meth:`Blueprint.route <Blueprint.route>`.
        """
        self._converters.append((converter, conv_type, conv_format))
        # Register converter in spec if app is already initialized
        if self.spec is not None:
            self._register_converter(converter, conv_type, conv_format)

    def _register_converter(self, converter, conv_type, conv_format=None):
        self.flask_plugin.register_converter(converter, conv_type, conv_format)

    def register_field(self, field, *args):
        """Register custom Marshmallow field

        Registering the Field class allows the Schema parser to set the proper
        type and format when documenting parameters from Schema fields.

        :param Field field: Marshmallow Field class

        ``*args`` can be:

        - a pair of the form ``(type, format)`` to map to
        - a core marshmallow field type (then that type's mapping is used)

        Examples: ::

            # Map to ('string', 'UUID')
            api.register_field(UUIDField, 'string', 'UUID')

            # Map to ('string')
            api.register_field(URLField, 'string', None)

            # Map to ('integer, 'int32')
            api.register_field(CustomIntegerField, ma.fields.Integer)

        Should be called before registering definitions with
        :meth:`definition <Api.definition>`.
        """
        self._fields.append((field, *args))
        # Register field in spec if app is already initialized
        if self.spec is not None:
            self._register_field(field, *args)

    def _register_field(self, field, *args):
        self.ma_plugin.map_to_openapi_type(*args)(field)


def _add_leading_slash(string):
    """Add leading slash to a string if there is None"""
    return string if string.startswith('/') else '/' + string


class DocBlueprintMixin:
    """Extend Api to serve the spec in a dedicated blueprint."""

    def _register_doc_blueprint(self):
        """Register a blueprint in the application to expose the spec

        Doc Blueprint contains routes to
        - json spec file
        - spec UI (ReDoc, Swagger UI).
        """
        api_url = self._app.config.get('OPENAPI_URL_PREFIX', None)
        if api_url is not None:
            blueprint = flask.Blueprint(
                'api-docs',
                __name__,
                url_prefix=_add_leading_slash(api_url),
                template_folder='./templates',
            )
            # Serve json spec at 'url_prefix/openapi.json' by default
            json_path = self._app.config.get(
                'OPENAPI_JSON_PATH', 'openapi.json')
            blueprint.add_url_rule(
                _add_leading_slash(json_path),
                endpoint='openapi_json',
                view_func=self._openapi_json)
            self._register_redoc_rule(blueprint)
            self._register_swagger_ui_rule(blueprint)
            self._app.register_blueprint(blueprint)

    def _register_redoc_rule(self, blueprint):
        """Register ReDoc rule

        The ReDoc script URL can be specified as OPENAPI_REDOC_URL.

        Otherwise, a CDN script is used based on the ReDoc version. The
        version can - and should - be specified as OPENAPI_REDOC_VERSION,
        otherwise, 'latest' is used.

        When using 1.x branch (i.e. when OPENAPI_REDOC_VERSION is "latest" or
        begins with "v1"), GitHub CDN is used.

        When using 2.x branch (i.e. when OPENAPI_REDOC_VERSION is "next" or
        begins with "2" or "v2"), unpkg nmp CDN is used.

        OPENAPI_REDOC_VERSION is ignored when OPENAPI_REDOC_URL is passed.
        """
        redoc_path = self._app.config.get('OPENAPI_REDOC_PATH')
        if redoc_path is not None:
            redoc_url = self._app.config.get('OPENAPI_REDOC_URL')
            if redoc_url is None:
                # TODO: default to 'next' when ReDoc 2.0.0 is released.
                redoc_version = self._app.config.get(
                    'OPENAPI_REDOC_VERSION', 'latest')
                # latest or v1.x -> Redoc GitHub CDN
                if redoc_version == 'latest' or redoc_version.startswith('v1'):
                    redoc_url = (
                        'https://rebilly.github.io/ReDoc/releases/'
                        '{}/redoc.min.js'.format(redoc_version))
                # next or 2.x -> unpkg npm CDN
                else:
                    redoc_url = (
                        'https://cdn.jsdelivr.net/npm/redoc@'
                        '{}/bundles/redoc.standalone.js'.format(redoc_version))
            self._redoc_url = redoc_url
            blueprint.add_url_rule(
                _add_leading_slash(redoc_path),
                endpoint='openapi_redoc',
                view_func=self._openapi_redoc)

    def _register_swagger_ui_rule(self, blueprint):
        """Register Swagger UI rule

        The Swagger UI scripts base URL can be specified as
        OPENAPI_SWAGGER_UI_URL.

        Otherwise, cdnjs is used. In this case, the Swagger UI version must be
        specified as OPENAPI_SWAGGER_UI_VERSION. Versions older than 3.x branch
        are not supported.

        OPENAPI_SWAGGER_UI_VERSION is ignored when OPENAPI_SWAGGER_UI_URL is
        passed.

        OPENAPI_SWAGGER_UI_SUPPORTED_SUBMIT_METHODS specifes the methods for
        which the 'Try it out!' feature is enabled.
        """
        swagger_ui_path = self._app.config.get('OPENAPI_SWAGGER_UI_PATH')
        if swagger_ui_path is not None:
            swagger_ui_url = self._app.config.get('OPENAPI_SWAGGER_UI_URL')
            if swagger_ui_url is None:
                swagger_ui_version = self._app.config.get(
                    'OPENAPI_SWAGGER_UI_VERSION')
                if swagger_ui_version is not None:
                    swagger_ui_url = (
                        'https://cdnjs.cloudflare.com/ajax/libs/swagger-ui/'
                        '{}/'.format(swagger_ui_version))
            if swagger_ui_url is not None:
                self._swagger_ui_url = swagger_ui_url
                self._swagger_ui_supported_submit_methods = (
                    self._app.config.get(
                        'OPENAPI_SWAGGER_UI_SUPPORTED_SUBMIT_METHODS',
                        ['get', 'put', 'post', 'delete', 'options',
                         'head', 'patch', 'trace'])
                )
                blueprint.add_url_rule(
                    _add_leading_slash(swagger_ui_path),
                    endpoint='openapi_swagger_ui',
                    view_func=self._openapi_swagger_ui)

    def _openapi_json(self):
        """Serve JSON spec file"""
        # We don't use Flask.jsonify here as it would sort the keys
        # alphabetically while we want to preserve the order.
        return current_app.response_class(
            json.dumps(self.spec.to_dict(), indent=2),
            mimetype='application/json')

    def _openapi_redoc(self):
        """Expose OpenAPI spec with ReDoc"""
        return flask.render_template(
            'redoc.html', title=self._app.name, redoc_url=self._redoc_url)

    def _openapi_swagger_ui(self):
        """Expose OpenAPI spec with Swagger UI"""
        return flask.render_template(
            'swagger_ui.html', title=self._app.name,
            swagger_ui_url=self._swagger_ui_url,
            swagger_ui_supported_submit_methods=(
                self._swagger_ui_supported_submit_methods)
        )
