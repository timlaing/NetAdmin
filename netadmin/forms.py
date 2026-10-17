"""CSRF-protected web forms."""

from flask_wtf import FlaskForm
from wtforms import BooleanField, PasswordField, SelectField, StringField, SubmitField
from wtforms.validators import DataRequired, EqualTo, Length, Regexp


class LoginForm(FlaskForm):
    """Login form."""

    username = StringField("Username", validators=[DataRequired(), Length(max=253)])
    password = PasswordField("Password", validators=[DataRequired(), Length(max=1024)])
    submit = SubmitField("Sign in")


class PasswordForm(FlaskForm):
    """Self-service password change form."""

    current_password = PasswordField("Current password", validators=[DataRequired()])
    new_password = PasswordField(
        "New password", validators=[DataRequired(), Length(min=12, max=1024)]
    )
    confirm_password = PasswordField(
        "Confirm new password", validators=[DataRequired(), EqualTo("new_password")]
    )
    submit = SubmitField("Change password")


class DnsRecordForm(FlaskForm):
    """DNS record mutation form."""

    zone = StringField("Zone", validators=[DataRequired(), Length(max=253)])
    name = StringField("Name", validators=[DataRequired(), Length(max=253)])
    record_type = SelectField(
        "Type",
        choices=[("A", "A"), ("AAAA", "AAAA"), ("PTR", "PTR"), ("CNAME", "CNAME"), ("TXT", "TXT")],
    )
    value = StringField("Value", validators=[DataRequired(), Length(max=1024)])
    ttl = StringField("TTL", default="300", validators=[Regexp(r"^[1-9][0-9]{0,8}$")])
    submit = SubmitField("Save record")


class DeleteDnsForm(FlaskForm):
    """DNS record deletion form."""

    zone = StringField(validators=[DataRequired()])
    name = StringField(validators=[DataRequired()])
    record_type = StringField(validators=[DataRequired()])
    value = StringField()
    submit = SubmitField("Delete")


class ReservationForm(FlaskForm):
    """DHCP reservation mutation form."""

    name = StringField("Host name", validators=[DataRequired(), Regexp(r"^[A-Za-z0-9_-]+$")])
    mac = StringField(
        "MAC address",
        validators=[DataRequired(), Regexp(r"^(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")],
    )
    address = StringField("IPv4 address", validators=[DataRequired()])
    submit = SubmitField("Save reservation")


class RadiusUserForm(FlaskForm):
    """RADIUS user mutation form."""

    username = StringField("Username", validators=[DataRequired(), Regexp(r"^[A-Za-z0-9_.@-]+$")])
    password = PasswordField("Password", validators=[Length(min=12, max=1024)])
    role = SelectField("Role", choices=[("standard", "Standard"), ("admin", "Administrator")])
    disabled = BooleanField("Disabled")
    submit = SubmitField("Save user")
