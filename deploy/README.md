# One-day demo on a rented server

Needs: any Linux VM with 4 CPU / 8 GB RAM / 20 GB disk and Docker. No GPU: the detector runs on CPU, the planner model runs at Google.

```bash
# on the VM
git clone <this repository> blueprint-qa && cd blueprint-qa
docker build -t blueprint-chat .
docker run -d --name chat --restart unless-stopped -p 127.0.0.1:8080:8080 \
  -e BP_ACCESS_CODE="$(openssl rand -hex 8 | tee /dev/stderr)" \
  -e GEMINI_API_KEY="<your key>" -e BP_MAX_MODEL_CALLS=400 \
  -v chatdata:/app/cache blueprint-chat
# HTTPS without a domain: a Cloudflare quick tunnel prints a https://....trycloudflare.com address
docker run -d --name tunnel --network host cloudflare/cloudflared:latest tunnel --no-autoupdate --url http://127.0.0.1:8080
docker logs tunnel 2>&1 | grep trycloudflare
```

Give mentors `https://<address>/?code=<the code printed above>`. Stop everything with `docker rm -f chat tunnel`, then delete the VM.

- The key is only in the container's environment. It is not in the image or the repository.
- Only the chat app is reachable; the access code is required for every page and API call.
- Uploads are limited to 60 MB and kept in a per-session folder inside the `chatdata` volume.
- After `BP_MAX_MODEL_CALLS` planner calls, questions the rules cannot read are refused with that reason.
