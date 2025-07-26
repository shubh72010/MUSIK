import discord
from discord.ext import commands, tasks
import asyncio
import yt_dlp
import os
import collections
import http.server # NEW: For the simple web server
import socketserver # NEW: For the simple web server
import threading    # NEW: To run the web server in a separate thread

# --- Configuration ---
# Get token from environment variable for secure deployment
TOKEN = os.getenv('DISCORD_BOT_TOKEN')
COMMAND_PREFIX = '!' # Command prefix for your bot

# --- Bot Setup ---
intents = discord.Intents.default()
intents.message_content = True # Required to read message content for commands
intents.voice_states = True    # Required for voice channel interactions

bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents)

# --- Global Data Structures ---
# A dictionary to hold voice clients for each guild (server)
voice_clients = {}
# A dictionary to hold music queues for each guild
music_queues = {}
# A dictionary to hold tasks for auto-disconnect, one per guild
disconnect_tasks = {}

# --- YTDL Options ---
YTDL_OPTIONS = {
    'format': 'bestaudio/best',
    'extractaudio': True,
    'audioformat': 'mp3',
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',
    'max_downloads': 1
}

# FFmpeg options for discord.FFmpegPCMAudio
FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn'
}

# --- YTDL Source Class (remains unchanged) ---
class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get('title')
        self.url = data.get('webpage_url')
        self.stream_url = data.get('url')

    @classmethod
    async def from_url(cls, url, *, loop=None, stream=True):
        loop = loop or asyncio.get_event_loop()
        ydl = yt_dlp.YoutubeDL(YTDL_OPTIONS)
        info = await loop.run_in_executor(None, lambda: ydl.extract_info(url, download=not stream))
        if 'entries' in info:
            info = info['entries'][0]
        source_url = info['url']
        return cls(discord.FFmpegPCMAudio(source_url, **FFMPEG_OPTIONS), data=info)

# --- Helper Functions (Private/Internal Use - remain largely unchanged) ---

async def _play_next_song(ctx):
    guild_id = ctx.guild.id
    if guild_id in disconnect_tasks and not disconnect_tasks[guild_id].done():
        disconnect_tasks[guild_id].cancel()
        print(f"[{ctx.guild.name}] Auto-disconnect task cancelled.")

    if guild_id in music_queues and music_queues[guild_id]:
        next_song_info = music_queues[guild_id].popleft()
        await ctx.send(f"Now playing: **{next_song_info['title']}** (Requested by: {next_song_info['requester'].mention})")
        print(f"[{ctx.guild.name}] Playing: {next_song_info['title']}")
        try:
            player = await YTDLSource.from_url(next_song_info['url'], loop=bot.loop, stream=True)
            voice_clients[guild_id].play(player, after=lambda e: bot.loop.create_task(_after_song_finished(ctx, e)))
        except Exception as e:
            await ctx.send(f"Error playing **{next_song_info['title']}**: {e}")
            print(f"[{ctx.guild.name}] Error playing song: {e}")
            if music_queues[guild_id]:
                await _play_next_song(ctx)
            else:
                await ctx.send("Queue finished or an error occurred with the last song.")
                await _start_auto_disconnect_task(ctx)
    else:
        await ctx.send("Queue finished. I will disconnect if idle.")
        print(f"[{ctx.guild.name}] Queue finished, scheduling auto-disconnect.")
        await _start_auto_disconnect_task(ctx)

async def _after_song_finished(ctx, error):
    if error:
        print(f"[{ctx.guild.name}] Player error: {error}")
        await ctx.send(f"An error occurred during playback: {error}")
    bot.loop.create_task(_play_next_song(ctx))

async def _start_auto_disconnect_task(ctx):
    guild_id = ctx.guild.id
    if guild_id in disconnect_tasks and not disconnect_tasks[guild_id].done():
        disconnect_tasks[guild_id].cancel()
        print(f"[{ctx.guild.name}] Existing auto-disconnect task cancelled for restart.")
    disconnect_tasks[guild_id] = bot.loop.create_task(_auto_disconnect_countdown(ctx))
    print(f"[{ctx.guild.name}] Auto-disconnect task scheduled.")

async def _auto_disconnect_countdown(ctx, delay_minutes=5):
    guild_id = ctx.guild.id
    try:
        await asyncio.sleep(delay_minutes * 60)
        if guild_id in voice_clients and voice_clients[guild_id].is_connected():
            if not voice_clients[guild_id].is_playing() and \
               (guild_id not in music_queues or not music_queues[guild_id]):
                await ctx.send(f"No activity for {delay_minutes} minutes. Leaving voice channel.")
                await voice_clients[guild_id].disconnect()
                del voice_clients[guild_id]
                if guild_id in music_queues:
                    del music_queues[guild_id]
                print(f"[{ctx.guild.name}] Auto-disconnected due to inactivity.")
            else:
                print(f"[{ctx.guild.name}] Auto-disconnect task aborted: Activity detected.")
        else:
            print(f"[{ctx.guild.name}] Auto-disconnect task finished, but bot not in voice channel.")
    except asyncio.CancelledError:
        print(f"[{ctx.guild.name}] Auto-disconnect task was cancelled.")
    except Exception as e:
        print(f"[{ctx.guild.name}] Error in auto-disconnect task: {e}")

# --- Bot Events (remain unchanged) ---
@bot.event
async def on_ready():
    print(f'Logged in as {bot.user.name} ({bot.user.id})')
    print('----------------------------------------------------')
    print('Bot is ready to receive commands.')

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"Error: Missing argument. Usage: `{COMMAND_PREFIX}{ctx.command.name} {ctx.command.signature}`")
    elif isinstance(error, commands.CommandNotFound):
        pass
    elif isinstance(error, commands.NotConnected):
        await ctx.send("I'm not connected to a voice channel. Use `!join` first.")
    elif isinstance(error, commands.NoPrivateMessage):
        await ctx.send("This command cannot be used in private messages.")
    elif isinstance(error, commands.CommandInvokeError):
        original = error.original
        if isinstance(original, yt_dlp.utils.DownloadError):
            await ctx.send(f"Could not find or process audio from that link/query: `{original}`. Please try a different one.")
            print(f"[{ctx.guild.name}] YTDL DownloadError: {original}")
        elif isinstance(original, discord.ClientException) and "Already playing" in str(original):
            pass
        else:
            await ctx.send(f"An unexpected error occurred during command execution: `{original}`")
            print(f"[{ctx.guild.name}] Unhandled CommandInvokeError: {original}")
            import traceback
            traceback.print_exc()
    else:
        print(f"[{ctx.guild.name}] An unhandled error occurred: {error}")
        await ctx.send(f"An unexpected error occurred: `{error}`")
        import traceback
        traceback.print_exc()

# --- Bot Commands (remain unchanged) ---
@bot.command(name='join', help='Makes the bot join your current voice channel.')
async def join(ctx):
    if not ctx.author.voice:
        await ctx.send(f"{ctx.author.mention}, you need to be in a voice channel for me to join.")
        return
    channel = ctx.author.voice.channel
    guild_id = ctx.guild.id
    try:
        if guild_id in voice_clients and voice_clients[guild_id].is_connected():
            if voice_clients[guild_id].channel != channel:
                await voice_clients[guild_id].move_to(channel)
                await ctx.send(f"Moved to **{channel.name}**.")
                print(f"[{ctx.guild.name}] Moved to voice channel: {channel.name}")
            else:
                await ctx.send(f"I'm already in **{channel.name}**.")
        else:
            voice_client = await channel.connect()
            voice_clients[guild_id] = voice_client
            await ctx.send(f"Joined **{channel.name}**.")
            print(f"[{ctx.guild.name}] Joined voice channel: {channel.name}")
            if guild_id not in music_queues:
                music_queues[guild_id] = collections.deque()
    except discord.ClientException as e:
        await ctx.send(f"Could not join voice channel: `{e}`. Make sure I have permissions.")
        print(f"[{ctx.guild.name}] ClientException joining channel: {e}")
    except asyncio.TimeoutError:
        await ctx.send("Timed out trying to connect to voice channel.")
        print(f"[{ctx.guild.name}] Timeout connecting to channel.")
    except Exception as e:
        await ctx.send(f"An unexpected error occurred while trying to join: `{e}`")
        print(f"[{ctx.guild.name}] Error in join command: {e}")

@bot.command(name='leave', help='Makes the bot leave the current voice channel.')
async def leave(ctx):
    guild_id = ctx.guild.id
    if guild_id in voice_clients and voice_clients[guild_id].is_connected():
        if voice_clients[guild_id].is_playing() or voice_clients[guild_id].is_paused():
            voice_clients[guild_id].stop()
            print(f"[{ctx.guild.name}] Stopped current playback.")
        if guild_id in music_queues:
            music_queues[guild_id].clear()
            del music_queues[guild_id]
            await ctx.send("Queue cleared.")
            print(f"[{ctx.guild.name}] Queue cleared.")
        if guild_id in disconnect_tasks and not disconnect_tasks[guild_id].done():
            disconnect_tasks[guild_id].cancel()
            del disconnect_tasks[guild_id]
            print(f"[{ctx.guild.name}] Auto-disconnect task cancelled and removed.")
        await voice_clients[guild_id].disconnect()
        del voice_clients[guild_id]
        await ctx.send("Left the voice channel.")
        print(f"[{ctx.guild.name}] Left voice channel.")
    else:
        await ctx.send("I am not currently in a voice channel.")

@bot.command(name='play', help='Plays a song from YouTube. Usage: !play [YouTube URL or search query]')
async def play(ctx, *, url: str):
    guild_id = ctx.guild.id
    if not ctx.author.voice:
        await ctx.send(f"{ctx.author.mention}, you need to be in a voice channel for me to play music.")
        return
    if guild_id not in voice_clients or not voice_clients[guild_id].is_connected():
        await ctx.invoke(bot.get_command('join'))
        await asyncio.sleep(1.5)
        if guild_id not in voice_clients or not voice_clients[guild_id].is_connected():
            await ctx.send("I couldn't join your voice channel. Please try `!join` first manually if problems persist.")
            return
    voice_client = voice_clients[guild_id]
    if guild_id not in music_queues:
        music_queues[guild_id] = collections.deque()
    await ctx.send(f"Searching for **{url}**...")
    print(f"[{ctx.guild.name}] Searching for: {url}")
    try:
        ydl = yt_dlp.YoutubeDL(YTDL_OPTIONS)
        info = await bot.loop.run_in_executor(None, lambda: ydl.extract_info(url, download=False))
        if 'entries' in info:
            info = info['entries'][0]
        song_info = {
            'url': info.get('webpage_url', url),
            'title': info.get('title', 'Unknown Title'),
            'duration': info.get('duration', 0),
            'requester': ctx.author
        }
        if voice_client.is_playing() or voice_client.is_paused():
            music_queues[guild_id].append(song_info)
            await ctx.send(f"Added **{song_info['title']}** to the queue. Position: `{len(music_queues[guild_id])}`.")
            print(f"[{ctx.guild.name}] Added to queue: {song_info['title']}")
        else:
            await ctx.send(f"Now playing: **{song_info['title']}** (Requested by: {song_info['requester'].mention})")
            print(f"[{ctx.guild.name}] Playing immediately: {song_info['title']}")
            player = await YTDLSource.from_url(song_info['url'], loop=bot.loop, stream=True)
            voice_client.play(player, after=lambda e: bot.loop.create_task(_after_song_finished(ctx, e)))
    except yt_dlp.utils.DownloadError as e:
        await ctx.send(f"Could not find or process that URL/query: `{e}`. Please try a different one.")
        print(f"[{ctx.guild.name}] YTDL DownloadError in play command: {e}")
    except Exception as e:
        await ctx.send(f"An unexpected error occurred while trying to play: `{e}`")
        print(f"[{ctx.guild.name}] Unexpected error in play command: {e}")
        import traceback
        traceback.print_exc()

@bot.command(name='pause', help='Pauses the current song.')
async def pause(ctx):
    guild_id = ctx.guild.id
    if guild_id in voice_clients and voice_clients[guild_id].is_connected():
        if voice_clients[guild_id].is_playing():
            voice_clients[guild_id].pause()
            await ctx.send("Playback paused.")
            print(f"[{ctx.guild.name}] Playback paused.")
        else:
            await ctx.send("No song is currently playing to pause.")
    else:
        await ctx.send("I am not currently in a voice channel.")

@bot.command(name='resume', help='Resumes the paused song.')
async def resume(ctx):
    guild_id = ctx.guild.id
    if guild_id in voice_clients and voice_clients[guild_id].is_connected():
        if voice_clients[guild_id].is_paused():
            voice_clients[guild_id].resume()
            await ctx.send("Playback resumed.")
            print(f"[{ctx.guild.name}] Playback resumed.")
        else:
            await ctx.send("No song is currently paused.")
    else:
        await ctx.send("I am not currently in a voice channel.")

@bot.command(name='stop', help='Stops the current song and clears the queue.')
async def stop(ctx):
    guild_id = ctx.guild.id
    if guild_id in voice_clients and voice_clients[guild_id].is_connected():
        if voice_clients[guild_id].is_playing() or voice_clients[guild_id].is_paused():
            voice_clients[guild_id].stop()
            await ctx.send("Stopped playback.")
            print(f"[{ctx.guild.name}] Playback stopped.")
        else:
            await ctx.send("Nothing is currently playing.")
        if guild_id in music_queues:
            music_queues[guild_id].clear()
            await ctx.send("Queue cleared.")
            print(f"[{ctx.guild.name}] Queue cleared.")
        await _start_auto_disconnect_task(ctx)
    else:
        await ctx.send("I am not currently in a voice channel or playing anything.")

@bot.command(name='skip', help='Skips the current song and plays the next in queue.')
async def skip(ctx):
    guild_id = ctx.guild.id
    if guild_id in voice_clients and voice_clients[guild_id].is_connected():
        if voice_clients[guild_id].is_playing() or voice_clients[guild_id].is_paused():
            voice_clients[guild_id].stop()
            await ctx.send("Skipped current song.")
            print(f"[{ctx.guild.name}] Skipped current song.")
        else:
            await ctx.send("No song is currently playing to skip.")
    else:
        await ctx.send("I am not currently in a voice channel.")

@bot.command(name='queue', aliases=['q'], help='Shows the current music queue.')
async def show_queue(ctx):
    guild_id = ctx.guild.id
    if guild_id in music_queues and music_queues[guild_id]:
        queue_display_limit = 10
        queue_list = []
        for i, song in enumerate(list(music_queues[guild_id])[:queue_display_limit]):
            duration_str = ''
            if song.get('duration'):
                minutes = song['duration'] // 60
                seconds = song['duration'] % 60
                duration_str = f" (`{minutes:02d}:{seconds:02d}`)"
            queue_list.append(f"{i+1}. {song['title']}{duration_str} (Requested by: {song['requester'].display_name})")
        
        response = f"**Current Queue:**\n```\n" + "\n".join(queue_list) + "\n```"
        if len(music_queues[guild_id]) > queue_display_limit:
            response += f"\nAnd {len(music_queues[guild_id]) - queue_display_limit} more songs..."
        await ctx.send(response)
    else:
        await ctx.send("The queue is currently empty. Use `!play` to add songs!")

@bot.command(name='nowplaying', aliases=['np', 'current'], help='Shows information about the currently playing song.')
async def now_playing(ctx):
    guild_id = ctx.guild.id
    if guild_id in voice_clients and voice_clients[guild_id].is_connected() and voice_clients[guild_id].is_playing():
        player_source = voice_clients[guild_id].source
        if isinstance(player_source, YTDLSource):
            title = player_source.title
            url = player_source.url
            data = player_source.data 
            duration = data.get('duration', 0)
            requester = data.get('requester')
            minutes = duration // 60
            seconds = duration % 60
            
            response = f"**Now Playing:** `{title}`\n"
            response += f"URL: <{url}>\n"
            response += f"Duration: `{minutes:02d}:{seconds:02d}`\n"
            if requester:
                 response += f"Requested by: {requester.mention}\n"
            await ctx.send(response)
        else:
            await ctx.send("Currently playing an unknown audio source.")
    else:
        await ctx.send("Nothing is currently playing.")

@bot.command(name='remove', help='Removes a song from the queue by its number. Usage: !remove <number>')
async def remove(ctx, index: int):
    guild_id = ctx.guild.id
    if guild_id not in music_queues or not music_queues[guild_id]:
        await ctx.send("The queue is empty, so there's nothing to remove.")
        return
    if not (1 <= index <= len(music_queues[guild_id])):
        await ctx.send(f"Invalid index. Please provide a number between 1 and {len(music_queues[guild_id])}.")
        return
    try:
        queue_list = list(music_queues[guild_id])
        removed_song = queue_list.pop(index - 1)
        music_queues[guild_id] = collections.deque(queue_list)
        await ctx.send(f"Removed **{removed_song['title']}** from the queue.")
        print(f"[{ctx.guild.name}] Removed '{removed_song['title']}' from queue.")
    except IndexError:
        await ctx.send("Could not remove song. The index might be out of range.")
    except Exception as e:
        await ctx.send(f"An error occurred while trying to remove the song: `{e}`")
        print(f"[{ctx.guild.name}] Error removing song: {e}")

@bot.command(name='clearqueue', aliases=['cq'], help='Clears all songs from the queue.')
async def clear_queue(ctx):
    guild_id = ctx.guild.id
    if guild_id in music_queues and music_queues[guild_id]:
        music_queues[guild_id].clear()
        await ctx.send("The entire queue has been cleared.")
        print(f"[{ctx.guild.name}] Entire queue cleared.")
    else:
        await ctx.send("The queue is already empty.")

# --- Web Server for Render Health Checks ---
class HealthCheckHandler(http.server.SimpleHTTPRequestHandler):
    """
    A simple HTTP request handler that always returns a 200 OK.
    This is to satisfy Render's health checks for a Web Service.
    """
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.end_headers()
        self.wfile.write(b"Bot is alive!")

def run_web_server():
    """
    Function to run the simple web server.
    Render provides the PORT environment variable.
    """
    # Use 0.0.0.0 to bind to all available network interfaces
    # Render provides PORT, default to 8080 for local testing
    PORT = int(os.getenv("PORT", "8080")) 
    
    # Using socketserver.TCPServer to avoid blocking the main thread
    # Setting allow_reuse_address to True helps with restarts
    with socketserver.TCPServer(("", PORT), HealthCheckHandler) as httpd:
        print(f"Web server serving health check on port {PORT}")
        # serve_forever() blocks, so run this in a separate thread
        httpd.serve_forever()


# --- Run the Bot ---
if __name__ == '__main__':
    # Start the web server in a separate daemon thread
    # It needs to start before bot.run()
    web_server_thread = threading.Thread(target=run_web_server)
    web_server_thread.daemon = True # Allows the main program to exit even if this thread is running
    web_server_thread.start()
    
    if TOKEN is None:
        print("\n--- ERROR ---")
        print("DISCORD_BOT_TOKEN environment variable not set. Please set it on Render or your local environment.")
        print("-----------------\n")
    else:
        print("\n--- STARTING BOT ---")
        print("Using Web Service deployment on Render (requires simple web server for health checks).")
        print("Ensure you have installed all prerequisites:")
        print("1. Python libraries: `pip install discord.py PyNaCl yt-dlp` (handled by requirements.txt on Render)")
        print("2. FFmpeg: Should be available on Render's environment. No manual install needed usually.")
        print("3. Discord Bot Intents: 'Message Content Intent' and 'Voice State Intent' enabled in Developer Portal.")
        print("4. Bot token is correctly set as an environment variable (DISCORD_BOT_TOKEN).")
        print("-----------------\n")
        try:
            bot.run(TOKEN)
        except discord.LoginFailure:
            print("\n--- LOGIN FAILED ---")
            print("Invalid Bot Token provided. Check your DISCORD_BOT_TOKEN environment variable.")
            print("--------------------\n")
        except Exception as e:
            print(f"\n--- AN UNEXPECTED ERROR OCCURRED DURING BOT STARTUP ---")
            print(f"Error: {e}")
            import traceback
            traceback.print_exc()
            print("------------------------------------------------------\n")

 
