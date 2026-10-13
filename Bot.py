
import asyncio
import json
import os
import re
import shutil
import socket
import time
from pathlib import Path

import discord
import psutil
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN") or ""
ADMIN_IDS = {
    item.strip()
    for item in (os.getenv("ADMIN_IDS") or "").split(",")
    if item.strip()
}
PREFIX = os.getenv("PREFIX") or "!"
DATA_DIR = Path(os.getenv("DATA_DIR") or "./data")
CONFIG_FILE = DATA_DIR / "config.json"
VPS_FILE = DATA_DIR / "vps.json"

DATA_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_CONFIG = {
    "backend": "lxc",
    "ram": 2048,
    "cpu": 1,
    "disk": 10,
    "maxram": 8192,
    "maxcpu": 4,
    "maxdisk": 50,
    "os": "ubuntu:22.04",
    "maintenance": False,
}


def load_json(path: Path, default: dict) -> dict:
    try:
        with path.open("r", encoding="utf-8") as file:
            value = json.load(file)
        return value if isinstance(value, dict) else default.copy()
    except (OSError, json.JSONDecodeError):
        return default.copy()


def save_json(path: Path, value: dict) -> None:
    temp_path = path.with_suffix(".tmp")
    with temp_path.open("w", encoding="utf-8") as file:
        json.dump(value, file, indent=2)
    temp_path.replace(path)


config = DEFAULT_CONFIG | load_json(CONFIG_FILE, {})
vps_data = load_json(VPS_FILE, {})


def save_config() -> None:
    save_json(CONFIG_FILE, config)


def save_vps() -> None:
    save_json(VPS_FILE, vps_data)


def is_admin(user_id: int) -> bool:
    return str(user_id) in ADMIN_IDS


intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)


async def run_command(*args: str, timeout: int = 120) -> tuple[int, str]:
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
        return process.returncode or 0, output.decode(errors="replace").strip()
    except asyncio.TimeoutError:
        return 124, "Command timed out"
    except FileNotFoundError:
        return 127, f"Command not found: {args[0]}"
    except OSError as exc:
        return 1, str(exc)


async def progress_message(ctx: commands.Context, stages: list[str]) -> discord.Message:
    message = await ctx.send("Starting deployment...")
    total = len(stages)

    for index, stage in enumerate(stages, start=1):
        percent = int(index / total * 100)
        filled = int(percent / 10)
        bar = "█" * filled + "░" * (10 - filled)
        await message.edit(
            content=f"**Deployment progress**\n`[{bar}] {percent}%`\n{stage}"
        )

    return message


async def lxc_available() -> bool:
    return shutil.which("lxc") is not None


async def lxc_deploy(
    name: str,
    ram_mb: int,
    cpu: int,
    disk_gb: int,
    os_image: str,
    message: discord.Message,
) -> tuple[bool, str]:
    if not await lxc_available():
        return False, "LXC is not installed on this host."

    stages = [
        "Checking LXC",
        "Creating container",
        "Applying resource limits",
        "Starting container",
        "Checking container status",
    ]

    try:
        for index, stage in enumerate(stages):
            percent = int((index + 1) / len(stages) * 100)
            bar = "█" * (percent // 10) + "░" * (10 - percent // 10)
            await message.edit(
                content=f"**Deploying `{name}`**\n`[{bar}] {percent}%`\n{stage}"
            )

            if index == 0:
                if not shutil.which("lxc"):
                    return False, "LXC is not installed."

            elif index == 1:
                code, output = await run_command(
                    "lxc", "launch", os_image, name, timeout=300
                )
                if code != 0:
                    return False, output or "Failed to create container."

            elif index == 2:
                settings = [
                    ("limits.memory", f"{ram_mb}MB"),
                    ("limits.cpu", str(cpu)),
                ]

                for key, value in settings:
                    code, output = await run_command(
                        "lxc", "config", "set", name, key, value
                    )
                    if code != 0:
                        await run_command("lxc", "delete", "--force", name)
                        return False, output or f"Failed to set {key}."

                code, output = await run_command(
                    "lxc", "config", "device", "set",
                    name, "root", "size", f"{disk_gb}GB"
                )
                if code != 0:
                    await run_command("lxc", "delete", "--force", name)
                    return False, (
                        "Could not set disk size. Check the LXC root disk device "
                        f"and storage pool. Details: {output}"
                    )

            elif index == 3:
                code, output = await run_command("lxc", "start", name)
                if code != 0 and "already running" not in output.lower():
                    return False, output or "Failed to start container."

            elif index == 4:
                code, output = await run_command("lxc", "info", name)
                if code != 0:
                    return False, output or "Could not inspect container."

        return True, f"Container `{name}` deployed successfully."

    except Exception as exc:
        return False, str(exc)


async def lxc_action(name: str, action: str) -> tuple[bool, str]:
    code, output = await run_command("lxc", action, name)
    return code == 0, output or ("Success" if code == 0 else "Command failed")


@bot.event
async def on_ready() -> None:
    print(f"Logged in as {bot.user} ({bot.user.id})")


@bot.command()
async def help(ctx: commands.Context) -> None:
    await ctx.send(
        "**VPS Bot Commands**\n"
        f"`{PREFIX}deploy <name> [ram_mb] [cpu] [disk_gb] [os_image]`\n"
        f"`{PREFIX}vps`\n"
        f"`{PREFIX}localhost`\n"
        f"`{PREFIX}start <name>`\n"
        f"`{PREFIX}stop <name>`\n"
        f"`{PREFIX}restart <name>`\n"
        f"`{PREFIX}rebuild <name> <os_image>`\n"
        f"`{PREFIX}delete <name>`\n"
        f"`{PREFIX}admin`\n"
        f"`{PREFIX}autodeployset <setting> <value>`"
    )


@bot.command()
async def localhost(ctx: commands.Context) -> None:
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    cpu_count = psutil.cpu_count() or 1
    hostname = socket.gethostname()

    await ctx.send(
        f"**Host resources: `{hostname}`**\n"
        f"CPU: `{cpu_count}` cores\n"
        f"RAM: `{memory.used // (1024**2)} / "
        f"{memory.total // (1024**2)} MB used`\n"
        f"Disk: `{disk.used // (1024**3)} / "
        f"{disk.total // (1024**3)} GB used`\n"
        f"Backend: `{config['backend']}`"
    )


@bot.command()
async def admin(ctx: commands.Context) -> None:
    if not is_admin(ctx.author.id):
        await ctx.reply("You are not an admin.")
        return

    await ctx.send(
        "**Admin Settings**\n"
        f"Backend: `{config['backend']}`\n"
        f"Default RAM: `{config['ram']} MB`\n"
        f"Default CPU: `{config['cpu']}`\n"
        f"Default Disk: `{config['disk']} GB`\n"
        f"Max RAM: `{config['maxram']} MB`\n"
        f"Max CPU: `{config['maxcpu']}`\n"
        f"Max Disk: `{config['maxdisk']} GB`\n"
        f"OS: `{config['os']}`\n"
        f"Maintenance: `{config['maintenance']}`\n\n"
        f"Use `{PREFIX}autodeployset <setting> <value>`"
    )


@bot.command()
async def autodeployset(
    ctx: commands.Context, setting: str = "", *, value: str = ""
) -> None:
    if not is_admin(ctx.author.id):
        await ctx.reply("You are not an admin.")
        return

    setting = setting.lower().strip()
    value = value.strip()

    allowed = {
        "ram", "cpu", "disk", "maxram", "maxcpu", "maxdisk",
        "backend", "os", "maintenance",
    }

    if setting not in allowed or not value:
        await ctx.reply(
            "Settings: `ram`, `cpu`, `disk`, `maxram`, `maxcpu`, "
            "`maxdisk`, `backend`, `os`, `maintenance`"
        )
        return

    if setting == "backend":
        if value.lower() not in {"lxc", "kvm"}:
            await ctx.reply("Backend must be `lxc` or `kvm`.")
            return
        config["backend"] = value.lower()

    elif setting == "os":
        if not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,100}", value):
            await ctx.reply("Invalid OS image name.")
            return
        config["os"] = value

    elif setting == "maintenance":
        if value.lower() not in {"true", "false", "on", "off"}:
            await ctx.reply("Use `true` or `false`.")
            return
        config["maintenance"] = value.lower() in {"true", "on"}

    else:
        try:
            number = int(value)
        except ValueError:
            await ctx.reply("Enter a valid number.")
            return

        if number < 1:
            await ctx.reply("Value must be greater than zero.")
            return

        config[setting] = number

    save_config()
    await ctx.reply(f"Updated `{setting}` to `{config[setting]}`.")


@bot.command()
async def deploy(ctx: commands.Context, name: str = "", ram: int = 0,
                 cpu: int = 0, disk: int = 0, *, os_image: str = "") -> None:
    if config["maintenance"] and not is_admin(ctx.author.id):
        await ctx.reply("Deployments are under maintenance.")
        return

    if not name or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9-]{1,30}", name):
        await ctx.reply(
            f"Usage: `{PREFIX}deploy <name> [ram_mb] [cpu] [disk_gb] [os_image]`"
        )
        return

    ram = ram or int(config["ram"])
    cpu = cpu or int(config["cpu"])
    disk = disk or int(config["disk"])
    os_image = os_image or str(config["os"])

    if ram > int(config["maxram"]) or cpu > int(config["maxcpu"]) or disk > int(config["maxdisk"]):
        await ctx.reply("Requested resources exceed the configured limits.")
        return

    if min(ram, cpu, disk) < 1:
        await ctx.reply("RAM, CPU and disk must be greater than zero.")
        return

    if name in vps_data:
        await ctx.reply("A VPS with that name is already registered.")
        return

    if config["backend"] == "kvm":
        await ctx.reply(
            "KVM is selected, but no KVM deployment adapter is configured. "
            "Install and configure libvirt/cloud-init on the host first."
        )
        return

    if config["backend"] != "lxc":
        await ctx.reply("Unsupported backend.")
        return

    message = await ctx.send("Preparing deployment...")
    success, result = await lxc_deploy(
        name, ram, cpu, disk, os_image, message
    )

    if not success:
        await message.edit(content=f"**Deployment failed**\n{result[:1500]}")
        return

    vps_data[name] = {
        "owner": ctx.author.id,
        "ram": ram,
        "cpu": cpu,
        "disk": disk,
        "os": os_image,
        "backend": "lxc",
        "created": int(time.time()),
    }
    save_vps()
    await message.edit(content=f"✅ {result}")


@bot.command()
async def vps(ctx: commands.Context) -> None:
    if not vps_data:
        await ctx.reply("No VPS instances are registered.")
        return

    lines = []
    for name, item in list(vps_data.items())[:30]:
        code, output = await run_command("lxc", "info", name)
        status = "Unknown"
        if code == 0:
            status = "Running" if "Status: RUNNING" in output else "Stopped"
        lines.append(
            f"`{name}` — {status} — {item['ram']}MB RAM, "
            f"{item['cpu']} CPU, {item['disk']}GB disk"
        )

    await ctx.send("\n".join(lines)[:1900])


async def manage_instance(
    ctx: commands.Context, name: str, action: str
) -> None:
    item = vps_data.get(name)
    if item is None:
        await ctx.reply("VPS not found.")
        return

    if item.get("owner") != ctx.author.id and not is_admin(ctx.author.id):
        await ctx.reply("You do not own this VPS.")
        return

    success, result = await lxc_action(name, action)
    await ctx.reply(
        f"✅ `{name}`: {action} successful."
        if success else f"❌ `{name}`: {result[:1000]}"
    )


@bot.command()
async def start(ctx: commands.Context, name: str) -> None:
    await manage_instance(ctx, name, "start")


@bot.command()
async def stop(ctx: commands.Context, name: str) -> None:
    await manage_instance(ctx, name, "stop")


@bot.command()
async def restart(ctx: commands.Context, name: str) -> None:
    await manage_instance(ctx, name, "restart")


@bot.command()
async def delete(ctx: commands.Context, name: str) -> None:
    item = vps_data.get(name)
    if item is None:
        await ctx.reply("VPS not found.")
        return

    if item.get("owner") != ctx.author.id and not is_admin(ctx.author.id):
        await ctx.reply("You do not own this VPS.")
        return

    await ctx.reply(
        f"To confirm, run `{PREFIX}confirmdelete {name}` within 60 seconds."
    )
    bot.pending_deletes[(ctx.author.id, name)] = time.time() + 60


@bot.command()
async def confirmdelete(ctx: commands.Context, name: str) -> None:
    key = (ctx.author.id, name)
    expiry = bot.pending_deletes.get(key, 0)

    if time.time() > expiry:
        await ctx.reply("No valid delete confirmation. Run the delete command again.")
        return

    item = vps_data.get(name)
    if item is None:
        await ctx.reply("VPS not found.")
        return

    if item.get("owner") != ctx.author.id and not is_admin(ctx.author.id):
        await ctx.reply("You do not own this VPS.")
        return

    success, result = await lxc_action(name, "delete")
    if success:
        vps_data.pop(name, None)
        save_vps()
        bot.pending_deletes.pop(key, None)
        await ctx.reply(f"Deleted `{name}`.")
    else:
        await ctx.reply(f"Delete failed: {result[:1000]}")


@bot.command()
async def rebuild(
    ctx: commands.Context, name: str, *, os_image: str = ""
) -> None:
    item = vps_data.get(name)
    if item is None:
        await ctx.reply("VPS not found.")
        return

    if item.get("owner") != ctx.author.id and not is_admin(ctx.author.id):
        await ctx.reply("You do not own this VPS.")
        return

    if not os_image:
        await ctx.reply(f"Usage: `{PREFIX}rebuild <name> <os_image>`")
        return

    if not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,100}", os_image):
        await ctx.reply("Invalid OS image name.")
        return

    await ctx.reply(
        "Rebuild deletes the existing container and its data. "
        f"To confirm, run `{PREFIX}confirmrebuild {name} {os_image}` within 60 seconds."
    )
    bot.pending_rebuilds[(ctx.author.id, name)] = {
        "expiry": time.time() + 60,
        "image": os_image,
    }


@bot.command()
async def confirmrebuild(
    ctx: commands.Context, name: str, *, os_image: str = ""
) -> None:
    key = (ctx.author.id, name)
    pending = bot.pending_rebuilds.get(key)

    if not pending or time.time() > pending["expiry"]:
        await ctx.reply("No valid rebuild confirmation.")
        return

    if pending["image"] != os_image:
        await ctx.reply("OS image does not match the confirmation.")
        return

    item = vps_data.get(name)
    if item is None:
        await ctx.reply("VPS not found.")
        return

    if item.get("owner") != ctx.author.id and not is_admin(ctx.author.id):
        await ctx.reply("You do not own this VPS.")
        return

    code, output = await run_command("lxc", "delete", "--force", name)
    if code != 0:
        await ctx.reply(f"Could not remove old container: {output[:1000]}")
        return

    success, result = await lxc_deploy(
        name,
        int(item["ram"]),
        int(item["cpu"]),
        int(item["disk"]),
        os_image,
        await ctx.send("Rebuilding VPS...")
    )

    if success:
        item["os"] = os_image
        save_vps()
        bot.pending_rebuilds.pop(key, None)
        await ctx.send(f"✅ Rebuilt `{name}`.")
    else:
        await ctx.send(f"❌ Rebuild failed: {result[:1000]}")


bot.pending_deletes = {}
bot.pending_rebuilds = {}


if not DISCORD_TOKEN:
    raise SystemExit("DISCORD_TOKEN is missing. Set it in your environment or .env file.")

bot.run(DISCORD_TOKEN)
