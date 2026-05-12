if __name__ == "__main__":
    import uvicorn
    print("\n🔥 YT-DLP Streaming Server Running (4K/8K Ready)")
    print("➡️  Open http://127.0.0.1:5000\n")
    uvicorn.run("app.main:app", host="0.0.0.0", port=5000)