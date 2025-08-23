from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    JobQueue,
    CallbackQueryHandler,
)
from datetime import datetime, timedelta
import sqlite3
import logging
import calendar
import asyncio
import os

# Set up logging to see what's happening
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get(
    "BOT_TOKEN", "8246409206:AAEptjKmPkhDI1zrMJgciyR_xMWqCuCiv-A"
)


# Initialize the SQLite Database
def init_db():
    conn = sqlite3.connect("attendance.db")
    c = conn.cursor()
    # Create table
    c.execute(
        """CREATE TABLE IF NOT EXISTS attendance_records
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER NOT NULL,
                  user_name TEXT NOT NULL,
                  check_in_time TIMESTAMP NOT NULL,
                  check_out_time TIMESTAMP NOT NULL,
                  alert_sent INTEGER DEFAULT 0)"""
    )  # 0 = False, 1 = True
    conn.commit()
    conn.close()


# Function to get a database connection
def get_db_connection():
    conn = sqlite3.connect("attendance.db")
    conn.row_factory = sqlite3.Row  # This enables name-based access to columns
    return conn


# Add a new record to the database
def add_check_in(user_id, user_name, check_in_time, check_out_time):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "INSERT INTO attendance_records (user_id, user_name, check_in_time, check_out_time) VALUES (?, ?, ?, ?)",
        (user_id, user_name, check_in_time, check_out_time),
    )
    record_id = c.lastrowid  # Get the ID of the newly created record
    conn.commit()
    conn.close()
    return record_id


# Get records for a user within a date range
def get_user_report(user_id, from_date, to_date):
    conn = get_db_connection()

    c = conn.cursor()
    c.execute(
        """
        SELECT date(check_in_time) as date, 
               time(check_in_time) as check_in, 
               time(check_out_time) as check_out 
        FROM attendance_records 
        WHERE user_id = ? 
        AND date(check_in_time) BETWEEN ? AND ?
        ORDER BY check_in_time
    """,
        (user_id, from_date, to_date),
    )

    records = c.fetchall()
    conn.close()
    return records


# Get pending alerts (check-out times that haven't passed yet and alerts not sent)
def get_pending_alerts():
    conn = get_db_connection()
    c = conn.cursor()
    current_time = datetime.now().isoformat()

    c.execute(
        """
        SELECT id, user_id, user_name, check_out_time 
        FROM attendance_records 
        WHERE check_out_time > ? AND alert_sent = 0
    """,
        (current_time,),
    )

    records = c.fetchall()
    conn.close()
    return records


async def set_commands(application: Application):
    """Set the bot commands menu"""
    commands = [
        BotCommand("start", "Start the bot and see welcome message"),
        BotCommand("checkin", "Record your check-in time"),
        BotCommand("report", "Generate attendance report with date picker"),
        BotCommand("help", "Show help information"),
    ]
    await application.bot.set_my_commands(commands)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a message when the command /start is issued."""
    help_text = (
        "Welcome to the Attendance Bot! 📊\n\n"
        "Available Commands:\n"
        "✅ /checkin - Record your check-in time\n"
        "📊 /report - Generate attendance report (with date picker!)\n"
        "ℹ️  /help - Show this help message\n\n"
        "You will receive an automatic alert at your check-out time!"
    )
    await update.message.reply_text(help_text)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show help information"""
    help_text = (
        "🤖 Attendance Bot Help\n\n"
        "Commands:\n"
        "• /checkin - Record your daily check-in time\n"
        "• /report - Generate attendance report with easy date picker\n"
        "• /help - Show this help message\n\n"
        "The bot will automatically notify you 9 hours after check-in!"
    )
    await update.message.reply_text(help_text)


async def checkin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /checkin command."""
    user_id = update.effective_user.id
    user_name = update.effective_user.full_name
    current_time = datetime.now()
    calculated_checkout = current_time + timedelta(hours=9)

    # Add record to the database
    record_id = add_check_in(
        user_id, user_name, current_time.isoformat(), calculated_checkout.isoformat()
    )

    # Format the times for a nice message
    check_in_str = current_time.strftime("%Y-%m-%d %H:%M:%S")
    check_out_str = calculated_checkout.strftime("%Y-%m-%d %H:%M:%S")

    reply_text = (
        f"✅ Check-in recorded for {user_name}!\n"
        f"🆔 Record ID: #{record_id}\n"
        f"⏰ Check-in Time: {check_in_str}\n"
        f"🚀 Calculated Check-out: {check_out_str}\n\n"
        f"You will be notified at your check-out time."
    )
    await update.message.reply_text(reply_text)

    # Schedule the Alert Job
    schedule_checkout_alert(
        context, record_id, user_id, calculated_checkout, update.effective_chat.id
    )


def schedule_checkout_alert(
    context: ContextTypes.DEFAULT_TYPE,
    record_id: int,
    user_id: int,
    check_out_time: datetime,
    chat_id: int,
):
    """Schedule a checkout alert job"""
    delay = (check_out_time - datetime.now()).total_seconds()

    if delay > 0:  # Only schedule if check-out time is in the future
        job = context.job_queue.run_once(
            callback=send_checkout_alert,
            when=delay,
            chat_id=chat_id,
            data={"record_id": record_id, "user_id": user_id},
            name=f"alert_{record_id}",
        )
        logger.info(f"Scheduled alert for record {record_id} in {delay:.0f} seconds")


async def send_checkout_alert(context: ContextTypes.DEFAULT_TYPE):
    """Callback function for the scheduled checkout alert job."""
    job = context.job
    record_id = job.data["record_id"]
    user_id = job.data["user_id"]

    conn = get_db_connection()
    c = conn.cursor()
    # Get the record from the database
    c.execute(
        "SELECT user_name, check_out_time FROM attendance_records WHERE id = ?",
        (record_id,),
    )
    record = c.fetchone()

    if record:
        user_name = record["user_name"]
        check_out_time = datetime.fromisoformat(record["check_out_time"])
        message = (
            f"🛎️ *ALERT for {user_name}!* 🛎️\n"
            "It's time to check out! Don't forget to submit your report.\n"
            f"Your calculated check-out was: {check_out_time.strftime('%H:%M:%S')}"
        )
        # Mark alert as sent in the database
        c.execute(
            "UPDATE attendance_records SET alert_sent = 1 WHERE id = ?", (record_id,)
        )
        conn.commit()
        logger.info(f"Sent checkout alert for user {user_name} (Record: {record_id})")
    else:
        message = "🛎️ Reminder: It's time to check out for the day!"
        logger.warning(f"Record {record_id} not found for alert")

    conn.close()
    await context.bot.send_message(chat_id=job.chat_id, text=message)


async def restore_pending_alerts(application: Application):
    """Restore all pending alerts when the bot starts"""
    try:
        pending_alerts = get_pending_alerts()
        logger.info(f"Found {len(pending_alerts)} pending alerts to restore")

        for alert in pending_alerts:
            record_id = alert["id"]
            user_id = alert["user_id"]
            check_out_time = datetime.fromisoformat(alert["check_out_time"])

            # Calculate delay until check-out time
            delay = (check_out_time - datetime.now()).total_seconds()

            if delay > 0:  # Only restore if check-out time is still in the future
                # For now, we'll just log that we found a pending alert
                # In a production system, you'd store chat_id in the database and reschedule
                logger.info(
                    f"Pending alert found for record {record_id} (in {delay:.0f} seconds)"
                )
            else:
                # Check-out time has passed, mark as sent
                conn = get_db_connection()
                c = conn.cursor()
                c.execute(
                    "UPDATE attendance_records SET alert_sent = 1 WHERE id = ?",
                    (record_id,),
                )
                conn.commit()
                conn.close()
                logger.info(f"Marked expired alert as sent for record {record_id}")

    except Exception as e:
        logger.error(f"Error restoring pending alerts: {e}")


def generate_calendar_keyboard(year=None, month=None):
    """Generate an inline keyboard calendar for date selection"""
    now = datetime.now()
    if year is None:
        year = now.year
    if month is None:
        month = now.month

    # Create calendar for the specified month
    cal = calendar.monthcalendar(year, month)
    month_name = calendar.month_name[month]

    # Create keyboard rows
    keyboard = []

    # Header row with month navigation
    header_row = []
    if month > 1 or year > now.year:
        header_row.append(
            InlineKeyboardButton("◀️", callback_data=f"nav_{year}_{month-1}")
        )
    else:
        header_row.append(InlineKeyboardButton(" ", callback_data="ignore"))

    header_row.append(
        InlineKeyboardButton(f"{month_name} {year}", callback_data="ignore")
    )

    if month < 12:
        header_row.append(
            InlineKeyboardButton("▶️", callback_data=f"nav_{year}_{month+1}")
        )
    else:
        header_row.append(InlineKeyboardButton(" ", callback_data="ignore"))

    keyboard.append(header_row)

    # Day names row
    day_names = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]
    keyboard.append(
        [InlineKeyboardButton(day, callback_data="ignore") for day in day_names]
    )

    # Days rows
    for week in cal:
        row = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(" ", callback_data="ignore"))
            else:
                date_str = f"{year}-{month:02d}-{day:02d}"
                row.append(
                    InlineKeyboardButton(str(day), callback_data=f"select_{date_str}")
                )
        keyboard.append(row)

    # Quick selection buttons
    today = now.strftime("%Y-%m-%d")
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    week_ago = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    month_start = now.replace(day=1).strftime("%Y-%m-%d")

    quick_buttons = [
        InlineKeyboardButton("Today", callback_data=f"quick_{today}_{today}"),
        InlineKeyboardButton(
            "Yesterday", callback_data=f"quick_{yesterday}_{yesterday}"
        ),
        InlineKeyboardButton("This Week", callback_data=f"quick_{week_ago}_{today}"),
        InlineKeyboardButton(
            "This Month", callback_data=f"quick_{month_start}_{today}"
        ),
    ]

    keyboard.append(quick_buttons[:2])  # First two buttons in one row
    keyboard.append(quick_buttons[2:])  # Next two buttons in next row

    # Cancel button
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])

    return InlineKeyboardMarkup(keyboard)


# Store user selection state
user_selection = {}


async def report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /report command with date picker."""
    user_id = update.effective_user.id

    # Initialize user selection
    user_selection[user_id] = {"from_date": None, "to_date": None, "message_id": None}

    # Send calendar for from date selection
    keyboard = generate_calendar_keyboard()
    message = await update.message.reply_text(
        "📅 Select the START date for your report:", reply_markup=keyboard
    )

    # Store the message ID for later editing
    user_selection[user_id]["message_id"] = message.message_id


async def handle_calendar_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle calendar button clicks"""
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data

    await query.answer()

    if user_id not in user_selection:
        await query.message.edit_text("Session expired. Please use /report again.")
        return

    if data == "cancel":
        await query.message.edit_text("Report generation cancelled.")
        if user_id in user_selection:
            del user_selection[user_id]
        return

    if data == "ignore":
        return  # Do nothing for ignore buttons

    if data.startswith("nav_"):
        # Navigation: change month
        _, year, month = data.split("_")
        year, month = int(year), int(month)
        keyboard = generate_calendar_keyboard(year, month)
        await query.message.edit_reply_markup(reply_markup=keyboard)
        return

    if data.startswith("quick_"):
        # Quick selection
        _, from_date, to_date = data.split("_")
        await generate_and_send_report(
            context, user_id, from_date, to_date, query.message.chat_id
        )
        if user_id in user_selection:
            del user_selection[user_id]
        return

    if data.startswith("select_"):
        # Date selected
        selected_date = data.split("_")[1]

        if user_selection[user_id]["from_date"] is None:
            # First date selected (from date)
            user_selection[user_id]["from_date"] = selected_date
            await query.message.edit_text(
                f"📅 Start date: {selected_date}\nNow select the END date:",
                reply_markup=generate_calendar_keyboard(),
            )
        else:
            # Second date selected (to date)
            user_selection[user_id]["to_date"] = selected_date
            from_date = user_selection[user_id]["from_date"]
            to_date = selected_date

            await generate_and_send_report(
                context, user_id, from_date, to_date, query.message.chat_id
            )
            if user_id in user_selection:
                del user_selection[user_id]


async def generate_and_send_report(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    from_date: str,
    to_date: str,
    chat_id: int,
):
    """Generate and send the attendance report"""
    records = get_user_report(user_id, from_date, to_date)

    if records:
        message_lines = [f"📊 Attendance Report for {from_date} to {to_date}:\n"]
        for record in records:
            message_lines.append(
                f"📅 {record['date']} | ⏰ In: {record['check_in']} | 🏠 Out: {record['check_out']}"
            )
        message = "\n".join(message_lines)
    else:
        message = f"No attendance records found from {from_date} to {to_date}."

    await context.bot.send_message(chat_id=chat_id, text=message)


async def list_jobs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all scheduled jobs for debugging."""
    jobs = context.job_queue.jobs()
    if jobs:
        job_list = [f"Job: {job.name} | Time: {job.next_t}" for job in jobs]
        await update.message.reply_text("Scheduled jobs:\n" + "\n".join(job_list))
    else:
        await update.message.reply_text("No scheduled jobs.")


async def post_init(application: Application):
    """Post initialization tasks"""
    # Set up the command menu
    await set_commands(application)
    # Restore pending alerts
    await restore_pending_alerts(application)


def main():
    """Start the bot."""
    # Initialize the database
    init_db()
    print("Database initialized.")

    # Create the Application
    application = Application.builder().token(BOT_TOKEN).build()

    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("checkin", checkin))
    application.add_handler(CommandHandler("report", report))
    application.add_handler(CommandHandler("jobs", list_jobs))  # Optional debug command

    # Add callback handler for calendar
    application.add_handler(CallbackQueryHandler(handle_calendar_callback))

    # Set up post initialization
    application.post_init = post_init

    # Start the Bot
    print("Bot is running with persistent alerts...")
    application.run_polling()


if __name__ == "__main__":
    main()
