from flask import Flask
from flask_mail import Mail, Message
import os

# --- Configuration for Testing ---
app = Flask(__name__)
app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = 'tanishqparashar2@gmail.com'
# Paste your NEW Google App Password here, being very careful with spacing.
app.config['MAIL_PASSWORD'] = 'frwytcnekntgpxgd'
app.config['MAIL_DEFAULT_SENDER'] = 'tanishqparashar2@gmail.com'

mail = Mail(app)

# --- Send Test Email Function ---
def send_test_email():
    with app.app_context():
        try:
            msg = Message(
                subject="Test Email from Flask-Mail",
                recipients=['tanishqparashar2@gmail.com'], # Change this to your email if it's different
                body="This is a test email to verify your Flask-Mail configuration is working."
            )
            mail.send(msg)
            print("Success! Test email sent.")
            return "Email sent successfully!"
        except Exception as e:
            print("Failed to send email. Here is the error:")
            print(e)
            return f"Failed to send email: {e}"

# --- Run the Script ---
if __name__ == "__main__":
    send_test_email()
