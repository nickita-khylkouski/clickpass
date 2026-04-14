My website is on the localhost currently but i would rather you use the actual site. It is my site and before YC we need a security audit.
Start in Chrome DevTools first on the actual target URL. Take a snapshot, inspect the network requests, and use Chrome DevTools MCP as the main browser path throughout the audit.
Do not spend the whole first pass on raw shell recon. If commands like `subfinder`, `amass`, or `ffuf` are noisy, save the full output to artifact files and work from a filtered preview.
Keep the first pass compact and evidence-driven:
- first try the real auth flow on the real site
- then inspect targeted network/API behavior
- only then widen recon
Do not pull giant artifacts into the conversation:
- do not `Read` full `.claude-config/.../tool-results/*.json` files
- do not paste full HTML pages or full JS bundles into context
- do not dump huge network lists into context repeatedly
- use bounded previews like `head -c 4000`, `sed -n '1,120p'`, `rg`, `grep`, `jq`, and targeted `get_network_request` calls
For Chrome network inspection, start small and targeted:
- use a small `pageSize`
- identify interesting `reqid`s
- inspect only those requests deeply
For JS bundle inspection:
- fetch one bundle at a time
- search for targeted strings like `api`, `/api/`, `graphql`, `auth`, `token`, `admin`, `swagger`, `openapi`, `stripe`, `subscription`, `customer`, `user`, `org`, and `workspace`
- do not load the entire bundle into the prompt unless it is short
When you start the scan, you should:
1. Find where you can signin and signup and if you are not able to make an account:
email: nickita@cerebralvalley.ai
Command to use it:

Paths:

- Launcher: gmail
- Main script: tools/gmail-agent-tool/gmail_agent_tool.py
- Wrapper: tools/gmail-agent-tool/gmail-agent

Usage:

- In this Daytona audit sandbox, do not start with `gmail auth`. A Gmail token is usually already preloaded.
- First try `gmail` or `gmail latest`, then `gmail read <MESSAGE_ID>`, then `gmail clean-raw --message-id <MESSAGE_ID>`.
- Only run `gmail auth --login-hint nickita@cerebralvalley.ai` if a Gmail command explicitly fails with an auth/credential error.
- gmail or gmail latest
- gmail search "example" --limit 5
- gmail read <MESSAGE_ID>
- gmail thread <THREAD_ID>
- gmail clean-raw --message-id <MESSAGE_ID> (strips noisy headers)

At first try to use headless chrome but if not then:
I'll launch a real visible Chrome debug instance (non-headless) on your Mac now with remote debugging enabled and open
2. Get subdomains using tools such as `subfinder` and optionally `amass`
3. using those subdomains try to find a way to signin and signup and if you are not able to make an account open a debugging instance of chrome for user to signin
3. Scan every single api route and check for vulnerabilities
4. Inspect all of the js bundles when posisble and check for vulnerabilities
5. Find the swagger and test all the api endpoints
6. Use Chrome DevTools MCP to especially test API routes.
7. Integrate with tools like `ffuf` or `nuclei` where appropriate.
8. Surface discovery:
- Get subdomains using tools such as `subfinder` and optionally `amass`

Use Chrome DevTools MCP to:
- scan every single api route and check for vulnerabilities
- inspect all of the js bundles when posisble and check for vulnerabilities
- either make an account or signup always. If you are not able to make an account open a debugging instance of chrome for user to signin

Thinsg that would make it to top 3 security issues:
- Get Acesss to paid content that shouldnt be accessible to the user
- Get access to the admin panel or an internal screen/api
- Get access to the database(using rest api or other methods)
These are the level of security issue that we are looking for and can be able to patch. These are also the main things to look for.
- get access to other users as this would be very big and we would have to patch

- Find the swagger and test all the api endpoints
Tools you have:
ALWAYS use Chrome DevTools MCP to especially test API routes.
- Integrate with tools like `ffuf` or `nuclei` where appropriate.
Surface discovery:
- Get subdomains using tools such as `subfinder` and optionally `amass`
- `subfinder`
- `httpx`
- Template and signature scanning:
- `nuclei`

For any findings that you find, you must validate it with at least some data to prove it is valid. and do not accept any findings that are not validated.
Once done output your top 3 most critical findings with evidnece/samples to support it.
