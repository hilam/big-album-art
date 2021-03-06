import os
import json

from furl import furl
import requests

from flask import Flask
from flask import session
from flask import redirect, request
from flask import render_template
from flask import url_for

from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, current_user, login_user, logout_user, login_required

from raven.contrib.flask import Sentry

import datetime

def get_env_variable(name):
    try:
        return os.environ[name]
    except KeyError:
        message = "Expected environment variable '{}' not set.".format(name)
        raise Exception(message)

# get env vars OR ELSE
POSTGRES_URL = get_env_variable("POSTGRES_URL") # 5432
POSTGRES_USER = get_env_variable("POSTGRES_USER")
POSTGRES_PW = get_env_variable("POSTGRES_PW")
POSTGRES_DB = get_env_variable("POSTGRES_DB")
REDIS_URL = get_env_variable("REDIS_URL") # 6379

SPOTIFY_CLIENT_ID = get_env_variable("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = get_env_variable("SPOTIFY_CLIENT_SECRET")
SPOTIFY_REDIRECT_URL = get_env_variable("SPOTIFY_REDIRECT_URL")

SPOTIFY_SCOPES = "user-read-private user-read-email user-read-playback-state user-read-currently-playing"

app = Flask(__name__)
app.config['SESSION_TYPE'] = 'redis'
app.config['SESSION_REDIS'] = REDIS_URL
app.secret_key = get_env_variable("SECRET_KEY")

SENTRY_DNS = get_env_variable("SENTRY_DNS")
sentry = None
if SENTRY_DNS != "nope":
    sentry = Sentry(app, dsn=SENTRY_DNS)

DB_URL = 'postgresql+psycopg2://{user}:{pw}@{url}/{db}'.format(user=POSTGRES_USER,pw=POSTGRES_PW,url=POSTGRES_URL,db=POSTGRES_DB)

app.config['SQLALCHEMY_DATABASE_URI'] = DB_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False # silence the deprecation warning

db = SQLAlchemy(app)
login_manager = LoginManager(app)

class User(db.Model):
    __tablename__ = "users" #not specifying this, lead to the table *not* being created. Duh.
    # postgres already has a 'user' table by default... Something went wrong here! (no errors)

    id = db.Column(db.Integer, primary_key=True)
    spotify_id = db.Column(db.String(200), unique=False, nullable=True)
    spotify_token = db.Column(db.String(200), unique=False, nullable=True)

    def is_authenticated(self):
        return True
    def is_active(self):
        return True
    def is_anonymous(self):
        return False
    def get_id(self):
        return self.spotify_id

@login_manager.user_loader
def load_user(user_id):
    return User.query.filter_by(spotify_id=user_id).first()

@app.cli.command('listusers')
def listusers_command():
    for user in User.query.all():
        print(user.spotify_id)

@app.cli.command('createdb')
def createdb_command():
    """Creates the database + tables."""
    from sqlalchemy_utils import database_exists, create_database

    if not database_exists(DB_URL):
        print('Creating database.')
        create_database(DB_URL)
    print('Creating tables.')
    db.create_all()
    print('Shiny!')

@app.cli.command('resetdb')
def resetdb_command():
    """Destroys and creates the database + tables."""

    from sqlalchemy_utils import database_exists, create_database, drop_database
    if database_exists(DB_URL):
        print('Deleting database.')
        drop_database(DB_URL)
    if not database_exists(DB_URL):
        print('Creating database.')
        create_database(DB_URL)

    #db.drop_all()
    print('Creating tables.')
    db.create_all()
    print('Shiny!')

def get_spotify_login_link():
    # https://developer.spotify.com/web-api/authorization-guide/#authorization-code-flow
    data = {
        "client_id": SPOTIFY_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": SPOTIFY_REDIRECT_URL,
        #"state": ,
        "scope": SPOTIFY_SCOPES,
    }
    url = "https://accounts.spotify.com/authorize"

    redirect_url = furl(url)
    redirect_url.args = data
    return redirect_url

def go_to_spotify():
    redirect_url = get_spotify_login_link()
    return redirect(redirect_url.url)

@app.route("/logout/")
@login_required
def logout():
    if current_user.is_authenticated:
        log_info = {
            "user_id": current_user.id,
            "spotify_id": current_user.spotify_id,
            "timestamp": datetime.datetime.utcnow().isoformat(),
            "action": "logout",
        }
        app.logger.info(json.dumps(log_info))
        logout_user()

    return redirect("/")

@app.route("/")
def start():
    if not current_user.is_authenticated:
        d = {}
        redirect_url = get_spotify_login_link()
        d["spotify_link"] = redirect_url.url
        d["piwik_on"] = os.environ.get("PIWIK_ON", False)
        return render_template("start.html", **d)

    d = get_data(current_user.spotify_token)
    if d == None:
        return go_to_spotify()

    return render_template("main.html", **d)

@app.route("/artist/<string:artist_id>")
@login_required
def artist_images(artist_id):
    # get albums of artist
    spotify_token = current_user.spotify_token

    images = []

    url = "https://api.spotify.com/v1/artists/{}".format(artist_id)

    headers = {'Authorization': "Bearer {}".format(spotify_token)}
    r = requests.get(url, headers=headers)

    if r.status_code == 204:
        return "whoops 204"

    parsed = json.loads(r.text)

    if len(parsed.get("images", [])) > 0:
        images.append(parsed["images"][0])

    # TODO: how to get to the gallery images?
    # TODO: how to get more visuals of the artist?

    image_data = list(map(lambda x: {
        "image_url": x["url"],
    }, images))

    d = {
        "images": image_data
    }

    return render_template("artist_images.html", **d)

@app.route("/artist/<string:artist_id>/albums/<string:display_type>")
@login_required
def artist_albums(artist_id, display_type):
    # get albums of artist
    spotify_token = current_user.spotify_token

    items = []

    done = False
    offset = 0

    while not done:
        url = "https://api.spotify.com/v1/artists/{}/albums?limit=50&offset={}".format(artist_id, offset)
        headers = {'Authorization': "Bearer {}".format(spotify_token)}
        r = requests.get(url, headers=headers)

        if r.status_code == 204:
            return "whoops 204"

        parsed = json.loads(r.text)

        # check if the token is still valid
        if parsed.get("items", None) == None:
            return "uh oh no items"

        items.extend(parsed["items"])

        if parsed["next"] is None:
            done = True
        else:
            offset += 50

    # TODO this is all kinds of terrible
    album_data = list(map(lambda x: {
        "image_url": x["images"][0]["url"],
        "release_date": x.get("release_date", "?"),
        "spotify_url": x["external_urls"].get("spotify", "?"),
        "type": x["album_type"],
    }, items))

    album_data.sort(key=lambda x: x["release_date"])

    if display_type == "categories" or display_type == "":
        d = {
            "albums": list(filter(lambda x: x["type"] == "album", album_data)),
            "singles": list(filter(lambda x: x["type"] == "single", album_data)),
            "compilations": list(filter(lambda x: x["type"] == "compilation", album_data)),
            "other": list(filter(lambda x: x["type"] not in ("album", "single", "compilation"), album_data)),
            "count": len(album_data),
            "other_url": url_for("artist_albums", artist_id=artist_id, display_type="chronological"),
        }
        # TODO: vue.js-up?
        return render_template("artist_albums_categories.html", **d)

    else:
        d = {
            "albums": album_data,
            "count": len(album_data),
            "other_url": url_for("artist_albums", artist_id=artist_id, display_type="categories"),
        }
        # TODO: vue.js-up?
        return render_template("artist_albums_chronological.html", **d)

def get_fake_data():
    return {
      "img_src": "https://vsupalov.com/images/avatar.png",

      #"artists": "Nobody",
      "artist_names_string": "Nobody",
      "album_name": "Album",
      "track_name": "Track",

      "track_ms_total": 100,
      "track_ms_progress": 20,
      "track_is_playing": True,
      "track_uri": "some uri",
    }

@app.route("/api/current/")
def api_current():
    d = get_fake_data()
    return json.dumps(d)

@app.route("/current/")
def current():
    d = get_data(current_user.spotify_token)
    if d == None:
        return json.dumps({"error": "relogin"})

    return json.dumps(d)

def get_data(spotify_token):
    url = "https://api.spotify.com/v1/me/player/currently-playing"
    headers = {'Authorization': "Bearer {}".format(spotify_token)}
    r = requests.get(url, headers=headers)
    # TODO: the above can return a 204, and I'm not handling that
    # --> caching previous responses is a good idea?

    if r.status_code == 204:
        return {
            "error": "nothing_playing",
            "nothing_playing": True
        }

    parsed = json.loads(r.text)

    # check if the token is still valid
    if parsed.get("item", None) == None:
        return None

    img_src = parsed["item"]["album"]["images"][0]["url"]

    return {
        "img_src": img_src,

        "artists": list(map(lambda x: {"name": x["name"], "id": x["id"]}, parsed["item"]["artists"])),
        "album_name": parsed["item"]["album"]["name"],
        "track_name": parsed["item"]["name"],

        "track_ms_total": parsed["item"]["duration_ms"],
        "track_ms_progress": parsed["progress_ms"],
        "track_is_playing": parsed["is_playing"],
        "track_uri": parsed["item"]["uri"],
    }

@app.route("/api/track/load/")
@login_required
def load_callback():
    log_info = {
        "user_id": current_user.id,
        "spotify_id": current_user.spotify_id,
        "timestamp": datetime.datetime.utcnow().isoformat(),
        "action": "reload",
    }
    app.logger.info(json.dumps(log_info))

@app.route("/api/track/tick-5m/")
@login_required
def tick_callback():
    log_info = {
        "user_id": current_user.id,
        "spotify_id": current_user.spotify_id,
        "timestamp": datetime.datetime.utcnow().isoformat(),
        "action": "tick-5m",
    }
    app.logger.info(json.dumps(log_info))

@app.route("/callback/")
def login_callback():
    code = request.args.get('code')
    error = request.args.get('error')
    state = request.args.get('state')
    #TODO: handle error cases?

    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": SPOTIFY_REDIRECT_URL,
        "client_id": SPOTIFY_CLIENT_ID,
        "client_secret": SPOTIFY_CLIENT_SECRET,
    }
    url = "https://accounts.spotify.com/api/token"

    r = requests.post(url, data=data)
    parsed = json.loads(r.text)

    token = parsed["access_token"]

    #get spotify user id
    url = "https://api.spotify.com/v1/me"
    headers = {'Authorization': "Bearer {}".format(token)}
    r = requests.get(url, headers=headers)
    parsed = json.loads(r.text)
    spotify_id = parsed["id"]

    user = User.query.filter_by(spotify_id=spotify_id).first()
    if not user is None:
        user.spotify_token = token
    else:
        user = User(spotify_id=spotify_id, spotify_token=token)
        print("Creating user! {}".format(spotify_id))

    db.session.add(user)
    #db.session.flush()
    db.session.commit()

    login_user(user)

    log_info = {
        "user_id": current_user.id,
        "spotify_id": current_user.spotify_id,
        "timestamp": datetime.datetime.utcnow().isoformat(),
        "action": "login",
    }
    app.logger.info(json.dumps(log_info))

    return redirect("/")

def noop_test():
    assert 1 == 1

if __name__ == "__main__":
    app.debug = True
    app.run()
