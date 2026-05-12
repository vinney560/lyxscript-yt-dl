#!/bin/bash
# Download yt-dlp binary for Linux
echo "📥 Downloading yt-dlp binary..."
curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_linux -o yt-dlp
chmod +x yt-dlp
echo "✅ yt-dlp binary installed"

# Install Node.js (required for yt-dlp's --js-runtime)
echo "📥 Installing Node.js..."
curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
apt-get install -y nodejs
echo "✅ Node.js $(node --version) installed"
