from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, SelectField, TextAreaField, IntegerField, FloatField, SubmitField
from wtforms.validators import DataRequired, Email, EqualTo, Length, Optional, ValidationError


class LoginForm(FlaskForm):
    email = StringField("Email", validators=[DataRequired(), Email()])
    password = PasswordField("Password", validators=[DataRequired(), Length(min=6)])
    submit = SubmitField("Log in")


class RegistrationForm(FlaskForm):
    name = StringField("Name", validators=[DataRequired(), Length(min=2, max=100)])
    email = StringField("Email", validators=[DataRequired(), Email()])
    password = PasswordField("Password", validators=[DataRequired(), Length(min=10)])
    confirm_password = PasswordField("Confirm password", validators=[DataRequired(), EqualTo("password")])

    def validate_password(self, field):
        value = field.data or ""
        if not any(character.isupper() for character in value):
            raise ValidationError("Password must contain an uppercase letter.")
        if not any(character.islower() for character in value):
            raise ValidationError("Password must contain a lowercase letter.")
        if not any(character.isdigit() for character in value):
            raise ValidationError("Password must contain a number.")
    submit = SubmitField("Create account")


class ConversionForm(FlaskForm):
    model_id = SelectField("Voice", choices=[], validators=[DataRequired()])
    pitch = IntegerField("Pitch", default=0)
    index_rate = FloatField("Index Rate", default=0.0)
    protect = FloatField("Protect", default=0.33)
    f0_method = SelectField("F0 Method", choices=[("rmvpe", "RMVPE"), ("pm", "PM")], default="rmvpe")
    output_format = SelectField("Output Format", choices=[("wav", "WAV"), ("mp3", "MP3")], default="wav")
    notes = TextAreaField("Notes", validators=[Optional()])
    submit = SubmitField("Convert")
