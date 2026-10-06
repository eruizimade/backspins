# Security

Backspins runs a small web server on your own computer (`127.0.0.1`, port
8765 or the next free one) and its window is a page served by it. That
server can read and change your rekordbox library, so it only answers its
own pages: requests whose `Host` is not the loopback address, whose `Origin`
is another site, or whose `Sec-Fetch-Site` is not same-origin are refused
with 403. It never listens on your network.

If you find a way around that, or anything else that lets someone other than
you reach your library or your files, please report it privately through
GitHub's **Report a vulnerability** button on the Security tab of this
repository rather than in a public issue.
