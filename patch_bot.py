with open('bot.py', 'r') as f:
    content = f.read()

# 1. Aggiungi import
if 'from leagues_data import ALL_LEAGUES' not in content:
    content = content.replace(
        'from poisson_engine import expected_goals, SERIE_A_2025_26, prob_1x2, prob_over_under, prob_btts',
        'from poisson_engine import expected_goals, prob_1x2, prob_over_under, prob_btts\nfrom leagues_data import ALL_LEAGUES'
    )

# 2. Modifica controllo squadre
old_check = 'if home not in SERIE_A_2025_26 or away not in SERIE_A_2025_26:'
new_check = '''    # Cerca squadra in tutti i campionati
    all_teams = set()
    for lt in ALL_LEAGUES.values():
        all_teams.update(lt.keys())
    if home not in all_teams or away not in all_teams:'''
content = content.replace(old_check, new_check)

# 3. Aggiungi comando /campionati
campionati_func = '''
async def cmd_campionati(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = "🏆 *Campionati disponibili*\\n━━━━━━━━━━━━━━━━━━━━━━\\n\\n"
    for name, teams in ALL_LEAGUES.items():
        msg += f"• *{name}*: {len(teams)} squadre\\n"
    msg += "\\nEsempio: `/segnale Manchester City Arsenal` (Premier League)"
    await update.message.reply_text(msg, parse_mode="Markdown")
'''
if 'cmd_campionati' not in content:
    content = content.replace('async def cmd_help', campionati_func + '\nasync def cmd_help')

# 4. Aggiungi handler
if 'CommandHandler("campionati"' not in content:
    content = content.replace(
        'application.add_handler(CommandHandler("help", cmd_help))',
        'application.add_handler(CommandHandler("campionati", cmd_campionati))\n    application.add_handler(CommandHandler("help", cmd_help))'
    )

with open('bot.py', 'w') as f:
    f.write(content)

print("✅ bot.py patchato per multi-campionato!")
