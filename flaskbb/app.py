# -*- coding: utf-8 -*-
"""
    flaskbb.app
    ~~~~~~~~~~~

    manages the app creation and configuration process

    :copyright: (c) 2014 by the FlaskBB Team.
    :license: BSD, see LICENSE for more details.
"""
import logging
import logging.config
import os
import sys
import time
import warnings
from datetime import datetime

from flask_restx import Api, Namespace, Resource, reqparse

from flask import Flask, request, jsonify
from flask_login import current_user
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError, ProgrammingError

# extensions
from flaskbb.extensions import (alembic, allows, babel, cache, celery, csrf,
                                db, debugtoolbar, limiter, login_manager, mail,
                                redis_store, themes, whooshee)
from flaskbb.plugins import spec
from flaskbb.plugins.manager import FlaskBBPluginManager
from flaskbb.plugins.models import PluginRegistry
from flaskbb.plugins.utils import remove_zombie_plugins_from_db, template_hook
# models
from flaskbb.user.models import Guest, User
from flaskbb.forum.models import Post, Topic, Forum
# various helpers
from flaskbb.utils.helpers import (app_config_from_env, crop_title,
                                   format_date, format_time, format_datetime,
                                   forum_is_unread, get_alembic_locations,
                                   get_flaskbb_config, is_online, mark_online,
                                   render_template, time_since, time_utcnow,
                                   topic_is_unread)
# permission checks (here they are used for the jinja filters)
from flaskbb.utils.requirements import (CanBanUser, CanEditUser, IsAdmin,
                                        IsAtleastModerator, can_delete_topic,
                                        can_edit_post, can_moderate,
                                        can_post_reply, can_post_topic,
                                        has_permission,
                                        permission_with_identity)
# whooshees
from flaskbb.utils.search import (ForumWhoosheer, PostWhoosheer,
                                  TopicWhoosheer, UserWhoosheer)
# app specific configurations
from flaskbb.utils.settings import flaskbb_config
from flaskbb.utils.translations import FlaskBBDomain

from sqlalchemy.exc import IntegrityError

from . import markup  # noqa
from .auth import views as auth_views  # noqa
from .deprecation import FlaskBBDeprecation
from .display.navigation import NavigationContentType
from .forum import views as forum_views  # noqa
from .management import views as management_views  # noqa
from .user import views as user_views  # noqa


logger = logging.getLogger(__name__)
api = Api(version='1.0', title='FlaskBB API',
              description='A simple FlaskBB API')
# API 네임스페이스 정의
api_namespace = Namespace('api', description='FlaskBB API operations')

parser = reqparse.RequestParser()
parser.add_argument('password', type=str, required=True, help='Password cannot be blank')
parser.add_argument('email', type=str, required=True, help='Email cannot be blank')
parser.add_argument('group_id', type=int, required=True, help='Group ID cannot be blank')
#parser.add_argument('email', type=str, required=True, help='Email cannot be blank')

# 간단한 리소스 추가
@api_namespace.route('/hello')
class HelloWorld(Resource):
    def get(self):
        return {'hello': 'world'}

@api_namespace.route('/users/<username>')
class ShowUserInfo(Resource):
    def get(self, username):
        user = User.query.filter_by(username=username).first_or_404()
        return {'username': user.username, 'email': user.email, 'group_id': user.primary_group_id}, 201

@api_namespace.route('/users')
class ListUsers(Resource):
    def get(self):
        users = User.query.all()
        user_list = []
        for user in users:
            user_info = {
                'username': user.username,
                'email': user.email,
                'group_id': user.primary_group_id
            }
            user_list.append(user_info)
        return user_list, 201

@api_namespace.route('/users/<username>')
class CreateUser(Resource):
    def post(self, username):
        try:
            existing_user = User.query.filter_by(username=username).first()
            if existing_user:
                return {'message': f'User {username} already exists.'}, 400

            args = parser.parse_args()
            new_user = User(username=username, password=args['password'], email=args['email'], primary_group_id=args['group_id'])

            db.session.add(new_user)
            db.session.commit()
            
            return {'message': f'User {username} has been created successfully.'}, 201
        except IntegrityError:
            db.session.rollback()
            return {'message': 'User data already exists.'}, 400

@api_namespace.route('/users/<username>')
class DeleteUser(Resource):
    def delete(self, username):
        user = User.query.filter_by(username=username).first()
        if not user:
            return {'message': f'User {username} does not exist.'}, 400

        db.session.delete(user)
        db.session.commit()
        
        return {'message': f'User {username} has been deleted successfully.'}, 201

@api_namespace.route('/delete_all_users')
class DeleteAllUsersExceptUbuntu(Resource):
    def delete(self):
        users_to_delete = User.query.filter(User.username != "ubuntu").all()
        
        if not users_to_delete:
            return {'message': 'No users to delete.'}, 400

        for user in users_to_delete:
            db.session.delete(user)
        
        db.session.commit()
        return {'message': 'All users except ubuntu have been deleted successfully.'}, 200

@api_namespace.route('/forums/topics')
class CreateTopic(Resource):
    def post(self):
        topic_parser = reqparse.RequestParser()
        topic_parser.add_argument('title', type=str, required=True, help='Title cannot be blank')
        topic_parser.add_argument('content', type=str, required=True, help='Content cannot be blank')
        topic_parser.add_argument('forum_id', type=int, required=True, help='Forum ID cannot be blank')
        topic_parser.add_argument('user_id', type=int, required=True, help='User ID cannot be blank')
                
        args = topic_parser.parse_args()
        
        # Forum 객체를 검색
        forum = Forum.query.get(args['forum_id'])
        if not forum:
            return {'message': 'Forum not found.'}, 404
        
        user = User.query.get(args['user_id'])
        if not user:
            return {'message': 'User not found.'}, 404

        # Topic 객체 생성 시 forum 객체 할당
        new_topic = Topic(title=args['title'])
        new_post = Post(content=args['content'], user=user, topic=new_topic)
        
        try:
            new_topic.save(user=user, forum=forum, post=new_post)
            return {'message': f'Topic {args["title"]} has been created successfully.'}, 201
        except IntegrityError:
            db.session.rollback()
            return {'message': 'Error creating topic.'}, 400

@api_namespace.route('/forums/topics/<int:topic_id>')
class DeleteTopic(Resource):
    def delete(self, topic_id):
        topic = Topic.query.get(topic_id)
        if not topic:
            return {'message': 'Topic not found.'}, 404

        topic.delete()
        return {'message': f'Topic {topic_id} has been deleted successfully.'}, 201

@api_namespace.route('/forums/topics')
class ListTopics(Resource):
    def get(self):
        topics = Topic.query.all()
        topic_ids = [topic.id for topic in topics]
        titles = [topic.title for topic in topics]
        return jsonify(topic_ids, titles)
    
@api_namespace.route('/forums')
class ListForums(Resource):
    def get(self):
        forums = Forum.query.all()
        forum_ids = [forum.id for forum in forums]
        titles = [forum.title for forum in forums]
        return jsonify(forum_ids, titles)

api.add_namespace(api_namespace)

def create_app(config=None, instance_path=None):
    """Creates the app.

    :param instance_path: An alternative instance path for the application.
                          By default the folder ``'instance'`` next to the
                          package or module is assumed to be the instance
                          path.
                          See :ref:`Instance Folders <flask:instance-folders>`.
    :param config: The configuration file or object.
                   The environment variable is weightet as the heaviest.
                   For example, if the config is specified via an file
                   and a ENVVAR, it will load the config via the file and
                   later overwrite it from the ENVVAR.
                   If no config is provided, FlaskBB will try to load the
                   config named ``flaskbb.cfg`` from the instance path.
    """

    app = Flask(
        "flaskbb", instance_path=instance_path, instance_relative_config=True
    )

    # instance folders are not automatically created by flask
    if not os.path.exists(app.instance_path):
        os.makedirs(app.instance_path)

    configure_app(app, config)
    configure_celery_app(app, celery)
    configure_extensions(app)
    load_plugins(app)
    configure_blueprints(app)
    configure_template_filters(app)
    configure_context_processors(app)
    configure_before_handlers(app)
    configure_errorhandlers(app)
    configure_migrations(app)
    configure_translations(app)
    app.pluggy.hook.flaskbb_additional_setup(app=app, pluggy=app.pluggy)

    # FlaskBB 앱에 api 객체를 초기화
    api.init_app(app)

    return app


def configure_app(app, config):
    """Configures FlaskBB."""
    # Use the default config and override it afterwards
    app.config.from_object("flaskbb.configs.default.DefaultConfig")
    config = get_flaskbb_config(app, config)
    # Path
    if isinstance(config, str):
        app.config.from_pyfile(config)
    # Module
    else:
        # try to update the config from the object
        app.config.from_object(config)

    # Add the location of the config to the config
    app.config["CONFIG_PATH"] = config

    # Environment
    # Get config file from envvar
    app.config.from_envvar("FLASKBB_SETTINGS", silent=True)

    # Parse the env for FLASKBB_ prefixed env variables and set
    # them on the config object
    app_config_from_env(app, prefix="FLASKBB_")

    # Migrate Celery 4.x config to Celery 6.x
    old_celery_config = app.config.get_namespace('CELERY_')
    celery_config = {}
    for key, value in old_celery_config.items():
        # config is the new format
        if key != "config":
            config_key = f"CELERY_{key.upper()}"
            celery_config[key] = value
            try:
                del app.config[config_key]
            except KeyError:
                pass

    # merge the new config with the old one
    new_celery_config = app.config.get("CELERY_CONFIG")
    new_celery_config.update(celery_config)
    app.config.update({"CELERY_CONFIG": new_celery_config})

    # Setting up logging as early as possible
    configure_logging(app)

    if not isinstance(config, str) and config is not None:
        config_name = "{}.{}".format(config.__module__, config.__name__)
    else:
        config_name = config

    logger.info("Using config from: {}".format(config_name))

    deprecation_level = app.config.get("DEPRECATION_LEVEL", "default")

    # never set the deprecation level during testing, pytest will handle it
    if not app.testing:  # pragma: no branch
        warnings.simplefilter(deprecation_level, FlaskBBDeprecation)

    debug_panels = app.config.setdefault(
        "DEBUG_TB_PANELS",
        [
            "flask_debugtoolbar.panels.versions.VersionDebugPanel",
            "flask_debugtoolbar.panels.timer.TimerDebugPanel",
            "flask_debugtoolbar.panels.headers.HeaderDebugPanel",
            "flask_debugtoolbar.panels.request_vars.RequestVarsDebugPanel",
            "flask_debugtoolbar.panels.config_vars.ConfigVarsDebugPanel",
            "flask_debugtoolbar.panels.template.TemplateDebugPanel",
            "flask_debugtoolbar.panels.sqlalchemy.SQLAlchemyDebugPanel",
            "flask_debugtoolbar.panels.logger.LoggingPanel",
            "flask_debugtoolbar.panels.route_list.RouteListDebugPanel",
            "flask_debugtoolbar.panels.profiler.ProfilerDebugPanel",
        ],
    )

    if all("WarningsPanel" not in p for p in debug_panels):
        debug_panels.append("flask_debugtoolbar_warnings.WarningsPanel")

    app.pluggy = FlaskBBPluginManager("flaskbb")


def configure_celery_app(app, celery):
    """Configures the celery app."""
    celery.conf.update(app.config.get("CELERY_CONFIG"))

    TaskBase = celery.Task

    class ContextTask(TaskBase):
        def __call__(self, *args, **kwargs):
            with app.app_context():
                return TaskBase.__call__(self, *args, **kwargs)

    celery.Task = ContextTask


def configure_blueprints(app):
    app.pluggy.hook.flaskbb_load_blueprints(app=app)

def configure_extensions(app):
    """Configures the extensions."""
    # Flask-Allows
    allows.init_app(app)
    allows.identity_loader(lambda: current_user)

    # Flask-WTF CSRF
    csrf.init_app(app)

    # Flask-SQLAlchemy
    db.init_app(app)

    # Flask-Alembic
    alembic.init_app(app, command_name="db")

    # Flask-Mail
    mail.init_app(app)

    # Flask-Cache
    cache.init_app(app)

    # Flask-Debugtoolbar
    debugtoolbar.init_app(app)

    # Flask-Themes
    themes.init_themes(app, app_identifier="flaskbb")

    # Flask-And-Redis
    redis_store.init_app(app)

    # Flask-Limiter
    limiter.init_app(app)

    # Flask-Whooshee
    whooshee.init_app(app)
    # not needed for unittests - and it will speed up testing A LOT
    if not app.testing:
        whooshee.register_whoosheer(PostWhoosheer)
        whooshee.register_whoosheer(TopicWhoosheer)
        whooshee.register_whoosheer(ForumWhoosheer)
        whooshee.register_whoosheer(UserWhoosheer)

    # Flask-Login
    login_manager.login_view = app.config["LOGIN_VIEW"]
    login_manager.refresh_view = app.config["REAUTH_VIEW"]
    login_manager.login_message_category = app.config["LOGIN_MESSAGE_CATEGORY"]
    login_manager.needs_refresh_message_category = app.config[
        "REFRESH_MESSAGE_CATEGORY"
    ]
    login_manager.anonymous_user = Guest

    @login_manager.user_loader
    def load_user(user_id):
        """Loads the user. Required by the `login` extension."""
        user_instance = User.query.filter_by(id=user_id).first()
        if user_instance:
            return user_instance
        else:
            return None

    login_manager.init_app(app)


def configure_template_filters(app):
    """Configures the template filters."""
    filters = {}

    filters["crop_title"] = crop_title
    filters["format_date"] = format_date
    filters["format_time"] = format_time
    filters["format_datetime"] = format_datetime
    filters["forum_is_unread"] = forum_is_unread
    filters["is_online"] = is_online
    filters["time_since"] = time_since
    filters["topic_is_unread"] = topic_is_unread

    permissions = [
        ("is_admin", IsAdmin),
        ("is_moderator", IsAtleastModerator),
        ("is_admin_or_moderator", IsAtleastModerator),
        ("can_edit_user", CanEditUser),
        ("can_ban_user", CanBanUser),
    ]

    filters.update(
        (name, permission_with_identity(perm, name=name))
        for name, perm in permissions
    )

    filters["can_moderate"] = can_moderate
    filters["post_reply"] = can_post_reply
    filters["edit_post"] = can_edit_post
    filters["delete_post"] = can_edit_post
    filters["post_topic"] = can_post_topic
    filters["delete_topic"] = can_delete_topic
    filters["has_permission"] = has_permission

    app.jinja_env.filters.update(filters)

    app.jinja_env.globals["run_hook"] = template_hook
    app.jinja_env.globals["NavigationContentType"] = NavigationContentType

    app.pluggy.hook.flaskbb_jinja_directives(app=app)


def configure_context_processors(app):
    """Configures the context processors."""

    @app.context_processor
    def inject_flaskbb_config():
        """Injects the ``flaskbb_config`` config variable into the
        templates.
        """
        return dict(flaskbb_config=flaskbb_config, format_date=format_date)

    @app.context_processor
    def inject_now():
        """Injects the current time."""
        return dict(now=datetime.utcnow())


def configure_before_handlers(app):
    """Configures the before request handlers."""

    @app.before_request
    def update_lastseen():
        """Updates `lastseen` before every reguest if the user is
        authenticated."""
        if current_user.is_authenticated:
            current_user.lastseen = time_utcnow()
            db.session.add(current_user)
            db.session.commit()

    if app.config["REDIS_ENABLED"]:

        @app.before_request
        def mark_current_user_online():
            if current_user.is_authenticated:
                mark_online(current_user.username)
            else:
                mark_online(request.remote_addr, guest=True)

    app.pluggy.hook.flaskbb_request_processors(app=app)


def configure_errorhandlers(app):
    """Configures the error handlers."""

    @app.errorhandler(403)
    def forbidden_page(error):
        return render_template("errors/forbidden_page.html"), 403

    @app.errorhandler(404)
    def page_not_found(error):
        return render_template("errors/page_not_found.html"), 404

    @app.errorhandler(500)
    def server_error_page(error):
        return render_template("errors/server_error.html"), 500

    app.pluggy.hook.flaskbb_errorhandlers(app=app)


def configure_migrations(app):
    """Configure migrations."""
    plugin_dirs = app.pluggy.hook.flaskbb_load_migrations()
    version_locations = get_alembic_locations(plugin_dirs)

    app.config["ALEMBIC"]["version_locations"] = version_locations


def configure_translations(app):
    """Configure translations."""

    # we have to initialize the extension after we have loaded the plugins
    # because we of the 'flaskbb_load_translations' hook
    babel.init_app(app=app, default_domain=FlaskBBDomain(app))

    @babel.localeselector
    def get_locale():
        # if a user is logged in, use the locale from the user settings
        if (
            current_user
            and current_user.is_authenticated
            and current_user.language
        ):
            return current_user.language
        # otherwise we will just fallback to the default language
        return flaskbb_config["DEFAULT_LANGUAGE"]


def configure_logging(app):
    """Configures logging."""
    if app.config.get("USE_DEFAULT_LOGGING"):
        configure_default_logging(app)

    if app.config.get("LOG_CONF_FILE"):
        logging.config.fileConfig(
            app.config["LOG_CONF_FILE"], disable_existing_loggers=False
        )

    if app.config["SQLALCHEMY_ECHO"]:
        # Ref: http://stackoverflow.com/a/8428546
        @event.listens_for(Engine, "before_cursor_execute")
        def before_cursor_execute(
            conn, cursor, statement, parameters, context, executemany
        ):
            conn.info.setdefault("query_start_time", []).append(time.time())

        @event.listens_for(Engine, "after_cursor_execute")
        def after_cursor_execute(
            conn, cursor, statement, parameters, context, executemany
        ):
            total = time.time() - conn.info["query_start_time"].pop(-1)
            app.logger.debug("Total Time: %f", total)


def configure_default_logging(app):
    # Load default logging config
    logging.config.dictConfig(app.config["LOG_DEFAULT_CONF"])

    if app.config["SEND_LOGS"]:
        configure_mail_logs(app)


def configure_mail_logs(app, formatter):
    from logging.handlers import SMTPHandler

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)-25s %(message)s"
    )
    mail_handler = SMTPHandler(
        app.config["MAIL_SERVER"],
        app.config["MAIL_DEFAULT_SENDER"],
        app.config["ADMINS"],
        "application error, no admins specified",
        (app.config["MAIL_USERNAME"], app.config["MAIL_PASSWORD"]),
    )

    mail_handler.setLevel(logging.ERROR)
    mail_handler.setFormatter(formatter)
    app.logger.addHandler(mail_handler)


def load_plugins(app):
    app.pluggy.add_hookspecs(spec)

    # have to find all the flaskbb modules that are loaded this way
    # otherwise sys.modules might change while we're iterating it
    # because of imports and that makes Python very unhappy
    # we are not interested in duplicated plugins or invalid ones
    # ('None' - appears on py2) and thus using a set
    flaskbb_modules = set(
        module
        for name, module in sys.modules.items()
        if name.startswith("flaskbb")
    )
    for module in flaskbb_modules:
        app.pluggy.register(module, internal=True)

    try:
        with app.app_context():
            plugins = PluginRegistry.query.all()

    except (OperationalError, ProgrammingError) as exc:
        logger.debug(
            "Database is not setup correctly or has not been " "setup yet.",
            exc_info=exc,
        )
        # load plugins even though the database isn't setup correctly
        # i.e. when creating the initial database and wanting to install
        # the plugins migration as well
        app.pluggy.load_setuptools_entrypoints("flaskbb_plugins")
        return

    for plugin in plugins:
        if not plugin.enabled:
            app.pluggy.set_blocked(plugin.name)

    app.pluggy.load_setuptools_entrypoints("flaskbb_plugins")
    app.pluggy.hook.flaskbb_extensions(app=app)

    loaded_names = set([p[0] for p in app.pluggy.list_name_plugin()])
    registered_names = set([p.name for p in plugins])
    unregistered = [
        PluginRegistry(name=name)
        for name in loaded_names - registered_names
        # ignore internal FlaskBB modules
        if not name.startswith("flaskbb.") and name != "flaskbb"
    ]
    with app.app_context():
        db.session.add_all(unregistered)
        db.session.commit()

        removed = 0
        if app.config["REMOVE_DEAD_PLUGINS"]:
            removed = remove_zombie_plugins_from_db()
            logger.info("Removed Plugins: {}".format(removed))

    # we need a copy of it because of
    # RuntimeError: dictionary changed size during iteration
    tasks = celery.tasks.copy()
    disabled_plugins = [
        p.__package__ for p in app.pluggy.get_disabled_plugins()
    ]
    for task_name, task in tasks.items():
        if task.__module__.split(".")[0] in disabled_plugins:
            logger.debug("Unregistering task: '{}'".format(task))
            celery.tasks.unregister(task_name)
