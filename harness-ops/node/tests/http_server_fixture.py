import threading


def start_http_server(server):
    thread = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    return thread
