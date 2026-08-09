import time

def run(args: dict, ctx) -> dict:
    seconds = args.get("seconds", 1.0)
    time.sleep(min(seconds, 10))  # cap to prevent the model stalling the whole loop for a long time
    return {"waited": seconds}