import json, os, urllib.parse
import requests
from flask import Flask, render_template, redirect, request, session, jsonify

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "change-me")

CLIENT_ID = os.getenv("CLIENT_ID", "YOUR_CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "YOUR_CLIENT_SECRET")
REDIRECT_URI = os.getenv("REDIRECT_URI", "http://localhost:5000/callback")
API = "https://discord.com/api"

COMMANDS = [
    {"name": "/ping", "desc": "Проверить задержку бота", "cat": "Общее"},
    {"name": "/warn", "desc": "Выдать предупреждение участнику", "cat": "Модерация"},
    {"name": "/mute", "desc": "Замутить участника", "cat": "Модерация"},
    {"name": "/ban", "desc": "Забанить участника", "cat": "Модерация"},
    {"name": "/room", "desc": "Создать приватный голосовой канал", "cat": "Комнаты"},
    {"name": "/ram", "desc": "Показать нагрузку сервера", "cat": "Общее"},
    {"name": ".ai", "desc": "Задать вопрос AI", "cat": "AI"},
]

@app.route("/")
def index():
    return render_template("index.html", user=session.get("user"))

@app.route("/commands")
def commands():
    return render_template("commands.html", commands=COMMANDS, user=session.get("user"))

@app.route("/stats")
def stats():
    return render_template("stats.html", user=session.get("user"))

@app.route("/api/stats")
def api_stats():
    try:
        with open("stats.json", encoding="utf-8") as f:
            return jsonify(json.load(f))
    except Exception:
        return jsonify({"guilds": 0, "users": 0})

@app.route("/invite")
def invite():
    q = urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "scope": "bot applications.commands",
        "permissions": 8,
    })
    return redirect(f"{API}/oauth2/authorize?{q}")

@app.route("/login")
def login():
    q = urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": "identify guilds",
    })
    return redirect(f"{API}/oauth2/authorize?{q}")

@app.route("/callback")
def callback():
    code = request.args.get("code")
    if not code:
        return redirect("/")
    r = requests.post(f"{API}/oauth2/token", data={
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
    }, headers={"Content-Type": "application/x-www-form-urlencoded"})
    token = r.json().get("access_token")
    if not token:
        return redirect("/")
    me = requests.get(f"{API}/users/@me", headers={"Authorization": f"Bearer {token}"}).json()
    session["user"] = {"id": me["id"], "name": me.get("global_name") or me["username"], "avatar": me.get("avatar")}
    return redirect("/")

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

if __name__ == "__main__":
    app.run(debug=True)