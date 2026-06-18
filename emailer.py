import smtplib
from email.message import EmailMessage


def send_results_email(sender_email, app_password, recipient_email, subject, body, csv_data):
    message = EmailMessage()
    message["From"] = sender_email
    message["To"] = recipient_email
    message["Subject"] = subject
    message.set_content(body)
    message.add_attachment(
        csv_data,
        subtype="csv",
        filename="stock_screener_results.csv",
    )

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(sender_email, app_password)
        smtp.send_message(message)
