# Ride checklist (laminate this)

1. Car: router on (12V), tablet wired, base up (`systemctl status convoy-base`
   or `make up-field`), open `http://<base>:8080/ops` — roster shows, cards red.
2. Bike on -> bridge boots (<=45 s). Headset ON near the bike. Card goes
   green; "headset: connected". Not in 60 s? Card's **BT ⟳**, then
   power-cycle the headset, then **reboot**.
3. Rider: phone on the convoy Wi-Fi, open `http://<base>:8080/rider`, pick
   your name, thumb **HOLD TO TALK** once — the ops card shows PTT.
4. Wait for the connected earcon (two rising tones). None in 15 s? Power-cycle headset.
5. Radio check in your room. Lead + chase confirm talk-over works (riders duck).
6. Fallback: GMRS ch __, Meshtastic on, hand signals reviewed.

On the road: lost a rider? Their card is red with "lost Ns ago" — try
**wifi ⟳** then **reboot** from the ops page; they hear earcons for each.
Wind beating the gate at speed? Riders hold TALK (PTT) — no threshold
changes on the fly.
