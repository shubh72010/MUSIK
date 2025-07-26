// Load environment variables from .env file
require('dotenv').config();

// Import necessary Discord.js classes
const { Client, GatewayIntentBits, Collection } = require('discord.js');
// --- CHANGE START ---
// Add StreamType to the import from @discordjs/voice
const { joinVoiceChannel, createAudioPlayer, createAudioResource, AudioPlayerStatus, StreamType } = require('@discordjs/voice');
// --- CHANGE END ---
const play = require('play-dl');
const ffmpegStatic = require('ffmpeg-static');
const youtubeDl = require('youtube-dl-exec');

// Import Node.js built-in modules for HTTP server (for Render Web Service)
const http = require('http');
const path = require('path');
const url = require('url');

// --- Configuration ---
const TOKEN = process.env.DISCORD_BOT_TOKEN;
const COMMAND_PREFIX = '!'; // Command prefix for your bot

// --- Bot Setup ---
const client = new Client({
    intents: [
        GatewayIntentBits.Guilds,
        GatewayIntentBits.GuildMessages,
        GatewayIntentBits.MessageContent, // REQUIRED for reading message content
        GatewayIntentBits.GuildVoiceStates, // REQUIRED for voice channel interactions
    ],
});

// Collections for managing voice connections and music queues
client.voiceConnections = new Collection(); // Map<guildId, VoiceConnection>
client.musicQueues = new Collection(); // Map<guildId, Array<songObject>>
client.audioPlayers = new Collection(); // Map<guildId, AudioPlayer>
client.disconnectTimers = new Collection(); // Map<guildId, setTimeoutId>

// --- Helper Functions (JavaScript equivalent) ---

async function playNextSong(guildId, textChannel) {
    const queue = client.musicQueues.get(guildId);
    const connection = client.voiceConnections.get(guildId);
    const player = client.audioPlayers.get(guildId);

    // Cancel any existing auto-disconnect timer
    if (client.disconnectTimers.has(guildId)) {
        clearTimeout(client.disconnectTimers.get(guildId));
        client.disconnectTimers.delete(guildId);
        console.log(`[${textChannel.guild.name}] Auto-disconnect timer cancelled.`);
    }

    if (!queue || queue.length === 0) {
        textChannel.send("Queue finished. I will disconnect if idle.");
        console.log(`[${textChannel.guild.name}] Queue finished, scheduling auto-disconnect.`);
        startAutoDisconnectTask(guildId, textChannel);
        return;
    }

    const song = queue.shift(); // Get the next song from the front of the queue
    textChannel.send(`Now playing: **${song.title}** (Requested by: ${song.requester.tag})`);
    console.log(`[${textChannel.guild.name}] Playing: ${song.title}`);

    try {
        const stream = youtubeDl.exec(song.url, {
            output: '-', // Output to stdout
            quiet: true, // Suppress console output
            format: 'bestaudio[ext=webm+acodec=opus]/bestaudio/best', // Prioritize opus in webm, then best audio
        }, {
            stdio: ['ignore', 'pipe', 'ignore'], // Stdin: ignore, Stdout: pipe, Stderr: ignore
        });

        // --- CHANGE START ---
        // Use StreamType from @discordjs/voice directly
        const resource = createAudioResource(stream.stdout, {
            inputType: StreamType.Arbitrary,
        });
        // --- CHANGE END ---

        player.play(resource);

        // Listen for player state changes
        player.once(AudioPlayerStatus.Idle, () => {
            console.log(`[${textChannel.guild.name}] Song finished, playing next.`);
            playNextSong(guildId, textChannel);
        });

        player.on('error', error => {
            console.error(`[${textChannel.guild.name}] Audio player error:`, error);
            textChannel.send(`An error occurred during playback of **${song.title}**: ${error.message}`);
            playNextSong(guildId, textChannel);
        });

    } catch (error) {
        textChannel.send(`Error playing **${song.title}**: ${error.message}`);
        console.error(`[${textChannel.guild.name}] Error creating audio resource:`, error);
        playNextSong(guildId, textChannel); // Try playing the next song
    }
}

function startAutoDisconnectTask(guildId, textChannel, delayMinutes = 5) {
    const delayMs = delayMinutes * 60 * 1000; // Convert minutes to milliseconds

    const timer = setTimeout(async () => {
        const connection = client.voiceConnections.get(guildId);
        const player = client.audioPlayers.get(guildId);
        const queue = client.musicQueues.get(guildId);

        if (connection && connection.state.status !== 'destroyed') {
            if (!player || player.state.status === AudioPlayerStatus.Idle && (!queue || queue.length === 0)) {
                textChannel.send(`No activity for ${delayMinutes} minutes. Leaving voice channel.`);
                connection.destroy(); // Disconnect from voice
                client.voiceConnections.delete(guildId);
                client.musicQueues.delete(guildId);
                client.audioPlayers.delete(guildId);
                console.log(`[${textChannel.guild.name}] Auto-disconnected due to inactivity.`);
            } else {
                console.log(`[${textChannel.guild.name}] Auto-disconnect task aborted: Activity detected.`);
                client.disconnectTimers.delete(guildId); // Remove timer as activity detected
            }
        } else {
            console.log(`[${textChannel.guild.name}] Auto-disconnect task finished, but bot not in voice channel.`);
            client.disconnectTimers.delete(guildId); // Remove timer if not connected
        }
    }, delayMs);

    client.disconnectTimers.set(guildId, timer);
    console.log(`[${textChannel.guild.name}] Auto-disconnect task scheduled for ${delayMinutes} minutes.`);
}

// --- Bot Events ---
client.once('ready', () => {
    console.log(`Logged in as ${client.user.tag}!`);
    console.log('----------------------------------------------------');
    console.log('Bot is ready to receive commands.');
    console.log(`Bot Prefix: ${COMMAND_PREFIX}`);
    console.log('Ensure you have enabled necessary intents in Developer Portal.');
});

client.on('messageCreate', async message => {
    // Ignore messages from bots and messages that don't start with the prefix
    if (message.author.bot || !message.content.startsWith(COMMAND_PREFIX)) return;

    const args = message.content.slice(COMMAND_PREFIX.length).trim().split(/ +/);
    const command = args.shift().toLowerCase(); // Get the command name

    const guildId = message.guild.id;
    const voiceChannel = message.member.voice.channel;
    let connection = client.voiceConnections.get(guildId);
    let player = client.audioPlayers.get(guildId);
    let queue = client.musicQueues.get(guildId);

    switch (command) {
        case 'join':
            if (!voiceChannel) {
                return message.reply('You need to be in a voice channel to make me join!');
            }
            if (connection && connection.state.status !== 'destroyed') {
                if (connection.joinConfig.channelId === voiceChannel.id) {
                    return message.reply(`I'm already in **${voiceChannel.name}**.`);
                } else {
                    connection.destroy();
                    client.voiceConnections.delete(guildId);
                    client.audioPlayers.delete(guildId);
                }
            }

            connection = joinVoiceChannel({
                channelId: voiceChannel.id,
                guildId: voiceChannel.guild.id,
                adapterCreator: voiceChannel.guild.voiceAdapterCreator,
                selfDeaf: true,
            });
            client.voiceConnections.set(guildId, connection);

            player = createAudioPlayer();
            client.audioPlayers.set(guildId, player);
            
            connection.subscribe(player);

            message.channel.send(`Joined **${voiceChannel.name}**.`);
            console.log(`[${message.guild.name}] Joined voice channel: ${voiceChannel.name}`);

            if (!queue) {
                client.musicQueues.set(guildId, []);
            }
            break;

        case 'leave':
            if (!connection || connection.state.status === 'destroyed') {
                return message.reply('I am not currently in a voice channel.');
            }

            if (player) {
                player.stop();
                client.audioPlayers.delete(guildId);
            }
            if (queue) {
                client.musicQueues.delete(guildId);
                message.channel.send("Queue cleared.");
            }
            if (client.disconnectTimers.has(guildId)) {
                clearTimeout(client.disconnectTimers.get(guildId));
                client.disconnectTimers.delete(guildId);
            }

            connection.destroy();
            client.voiceConnections.delete(guildId);
            message.channel.send('Left the voice channel.');
            console.log(`[${message.guild.name}] Left voice channel.`);
            break;

        case 'play':
            if (!voiceChannel) {
                return message.reply('You need to be in a voice channel to play music!');
            }
            if (!args.length) {
                return message.reply('You need to provide a search query!');
            }

            if (!connection || connection.state.status === 'destroyed') {
                try {
                    connection = joinVoiceChannel({
                        channelId: voiceChannel.id,
                        guildId: voiceChannel.guild.id,
                        adapterCreator: voiceChannel.guild.voiceAdapterCreator,
                        selfDeaf: true,
                    });
                    client.voiceConnections.set(guildId, connection);
                    player = createAudioPlayer();
                    client.audioPlayers.set(guildId, player);
                    connection.subscribe(player);
                    message.channel.send(`Joined **${voiceChannel.name}** to play music.`);
                } catch (error) {
                    console.error(`[${message.guild.name}] Error joining voice channel before play:`, error);
                    return message.channel.send("I couldn't join your voice channel to play music. Please ensure I have permissions.");
                }
            }

            const searchString = args.join(' ');
            message.channel.send(`Searching for **${searchString}**...`);
            console.log(`[${message.guild.name}] Searching for: ${searchString}`);

            try {
                // Reverted to searching all sources to get a more reliable result
                let results = await play.search(searchString, { limit: 1 });

                if (!results || results.length === 0) {
                    return message.reply(`Could not find any results for **${searchString}**. Please try a different query.`);
                }
                const songInfo = {
                    title: results[0].title,
                    url: results[0].url,
                    requester: message.author,
                };

                if (!queue) {
                    queue = [];
                    client.musicQueues.set(guildId, queue);
                }

                queue.push(songInfo);

                if (player.state.status !== AudioPlayerStatus.Playing && player.state.status !== AudioPlayerStatus.Buffering) {
                    await playNextSong(guildId, message.channel);
                } else {
                    message.channel.send(`Added **${songInfo.title}** to the queue. Position: \`${queue.length}\`.`);
                    console.log(`[${message.guild.name}] Added to queue: ${songInfo.title}`);
                }

            } catch (error) {
                message.reply(`Could not find or process audio from that link/query: \`${error.message}\`. Please try a different one.`);
                console.error(`[${message.guild.name}] Error in play command:`, error);
            }
            break;

        case 'pause':
            if (!player || player.state.status !== AudioPlayerStatus.Playing) {
                return message.reply('No song is currently playing to pause.');
            }
            player.pause();
            message.channel.send('Playback paused.');
            console.log(`[${message.guild.name}] Playback paused.`);
            break;

        case 'resume':
            if (!player || player.state.status !== AudioPlayerStatus.Paused) {
                return message.reply('No song is currently paused.');
            }
            player.unpause();
            message.channel.send('Playback resumed.');
            console.log(`[${message.guild.name}] Playback resumed.`);
            break;

        case 'stop':
            if (!player || (player.state.status !== AudioPlayerStatus.Playing && player.state.status !== AudioPlayerStatus.Paused)) {
                return message.reply('Nothing is currently playing.');
            }
            player.stop();
            if (queue) {
                queue.length = 0;
                message.channel.send('Queue cleared.');
                console.log(`[${message.guild.name}] Queue cleared.`);
            }
            startAutoDisconnectTask(guildId, message.channel);
            message.channel.send('Stopped playback.');
            console.log(`[${message.guild.name}] Playback stopped.`);
            break;

        case 'skip':
            if (!player || (player.state.status !== AudioPlayerStatus.Playing && player.state.status !== AudioPlayerStatus.Paused)) {
                return message.reply('No song is currently playing to skip.');
            }
            player.stop();
            message.channel.send('Skipped current song.');
            console.log(`[${message.guild.name}] Skipped current song.`);
            break;

        case 'queue':
        case 'q':
            if (!queue || queue.length === 0) {
                return message.channel.send("The queue is currently empty. Use `!play` to add songs!");
            }
            const queueDisplayLimit = 10;
            const queueList = queue.slice(0, queueDisplayLimit).map((song, index) =>
                `${index + 1}. ${song.title} (Requested by: ${song.requester.tag})`
            ).join('\n');

            let response = `**Current Queue:**\n\`\`\`\n${queueList}\n\`\`\``;
            if (queue.length > queueDisplayLimit) {
                response += `\nAnd ${queue.length - queueDisplayLimit} more songs...`;
            }
            message.channel.send(response);
            break;

        case 'nowplaying':
        case 'np':
        case 'current':
            if (!player || player.state.status === AudioPlayerStatus.Idle) {
                return message.channel.send("Nothing is currently playing.");
            }
            const currentSong = queue && queue.currentSong; // Placeholder if you add this logic
            if (currentSong) {
                 message.channel.send(`**Now Playing:** \`${currentSong.title}\` (Requested by: ${currentSong.requester.tag})`);
            } else {
                 message.channel.send("A song is currently playing!");
            }
            console.log(`[${message.guild.name}] Now playing status requested.`);
            break;

        case 'remove':
            if (!queue || queue.length === 0) {
                return message.reply("The queue is empty, so there's nothing to remove.");
            }
            const index = parseInt(args[0]);
            if (isNaN(index) || index < 1 || index > queue.length) {
                return message.reply(`Invalid index. Please provide a number between 1 and ${queue.length}.`);
            }
            const removedSong = queue.splice(index - 1, 1);
            message.channel.send(`Removed **${removedSong[0].title}** from the queue.`);
            console.log(`[${message.guild.name}] Removed '${removedSong[0].title}' from queue.`);
            break;

        case 'clearqueue':
        case 'cq':
            if (!queue || queue.length === 0) {
                return message.reply("The queue is already empty.");
            }
            queue.length = 0;
            message.channel.send("The entire queue has been cleared.");
            console.log(`[${message.guild.name}] Entire queue cleared.`);
            break;

        default:
            break;
    }
});

// --- Render Web Service Health Check ---
const server = http.createServer((req, res) => {
    if (req.url === '/') {
        res.writeHead(200, { 'Content-Type': 'text/plain' });
        res.end('Bot is alive!');
    } else {
        res.writeHead(404, { 'Content-Type': 'text/plain' });
        res.end('Not Found');
    }
});

const port = process.env.PORT || 8080;

server.listen(port, () => {
    console.log(`Web server listening on port ${port} for Render health checks.`);
});

// --- Login to Discord ---
if (!TOKEN) {
    console.error("\n--- ERROR ---");
    console.error("DISCORD_BOT_TOKEN environment variable not set. Please set it on Render or in your local .env file.");
    console.error("-----------------\n");
} else {
    console.log("\n--- STARTING BOT ---");
    console.log("Using Web Service deployment on Render (requires simple web server for health checks).");
    console.log("Ensure you have installed all prerequisites:");
    console.log("1. Node.js packages: `npm install discord.js dotenv @discordjs/voice play-dl ffmpeg-static youtube-dl-exec @ffmpeg-installer/ffmpeg` (handled by Render's build process)");
    console.log("2. FFmpeg: `ffmpeg-static` should provide it, or Render's environment often has it.");
    console.log("3. Discord Bot Intents: 'Message Content Intent' and 'Voice State Intent' enabled in Developer Portal.");
    console.log("4. Bot token is correctly set as an environment variable (DISCORD_BOT_TOKEN).");
    console.log("-----------------\n");
    client.login(TOKEN).catch(err => {
        console.error("\n--- LOGIN FAILED ---");
        console.error("Invalid Bot Token provided or network error. Check your DISCORD_BOT_TOKEN environment variable.");
        console.error(`Error: ${err.message}`);
        console.error("--------------------\n");
        process.exit(1);
    });
}
