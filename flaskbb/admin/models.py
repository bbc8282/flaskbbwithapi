from wtforms import (TextField, IntegerField, BooleanField, SelectField,
                     FloatField, validators)
from flask.ext.wtf import Form
from flaskbb.extensions import db


def normalize_to(value, value_type, reverse=False):
    """Converts a value to a specified value type.
    Available value types are: string, integer, boolean and array.
    A boolean type is handled as 0 for false and 1 for true.

    :param value: The value which should be converted.
    :param value_type: The value_type.
    :param reverse: If the value should be converted back
    """
    if reverse:
        if value_type == 'array':
            return value
        if value_type == 'integer':
            return int(value)
        if value_type == 'float':
            return float(value)
        if value_type == 'boolean':
            return value == "1"

        # text
        return value
    else:
        if value_type == 'array':
            return value.replace(" ", "").strip(",")
        if value_type == 'integer':
            return int(value)
        if value_type == 'float':
            return float(value)
        if value_type == 'boolean':
            return 1 if value else 0

        # text
        return value


class SettingsGroup(db.Model):
    __tablename__ = "settingsgroup"

    key = db.Column(db.String, primary_key=True)
    name = db.Column(db.String, nullable=False)
    description = db.Column(db.String, nullable=False)
    settings = db.relationship("Setting", lazy="dynamic", backref="group",
                               cascade="all, delete-orphan")

    def save(self):
        """Saves a settingsgroup."""
        db.session.add(self)
        db.session.commit()

    def delete(self):
        """Deletes a settingsgroup."""
        db.session.delete(self)
        db.session.commit()


class Setting(db.Model):
    __tablename__ = "settings"

    key = db.Column(db.String, primary_key=True)
    _value = db.Column("value", db.String, nullable=False)
    settingsgroup = db.Column(db.String,
                              db.ForeignKey('settingsgroup.key',
                                            use_alter=True,
                                            name="fk_settingsgroup"),
                              nullable=False)

    # The name (displayed in the form)
    name = db.Column(db.String, nullable=False)

    # The description (displayed in the form)
    description = db.Column(db.String, nullable=False)

    # Available types: string, integer, boolean, array, float
    value_type = db.Column(db.String, nullable=False)

    # Available types: text, number, choice, yesno
    # They are used in the form creation process
    input_type = db.Column(db.String, nullable=False)

    # Extra attributes like, validation things (min, max length...)
    extra = db.Column(db.PickleType)

    # Properties
    @property
    def value(self):
        return normalize_to(self._value, self.value_type)

    @value.setter
    def value(self, value):
        self._value = normalize_to(value, self.value_type, reverse=True)

    @classmethod
    def get_form(cls, group):
        """Returns a Form for all settings found in :class:`SettingsGroup`.

        :param group: The settingsgroup name. It is used to get the settings
                      which are in the specified group.
        """

        class SettingsForm(Form):
            pass

        # now parse that shit
        for setting in group.settings:
            field_validators = []

            # generate the validators
            # TODO: Do this in another function
            if "min" in setting.extra:
                # Min number validator
                if setting.value_type in ("integer", "float"):
                    field_validators.append(
                        validators.NumberRange(min=setting.extra["min"])
                    )

                # Min text length validator
                elif setting.value_type in ("string", "array"):
                    field_validators.append(
                        validators.Length(min=setting.extra["min"])
                    )

            if "max" in setting.extra:
                # Max number validator
                if setting.value_type in ("integer", "float"):
                    field_validators.append(
                        validators.NumberRange(max=setting.extra["max"])
                    )

                # Max text length validator
                elif setting.value_type in ("string", "array"):
                    field_validators.append(
                        validators.Length(max=setting.extra["max"])
                    )

            # Generate the fields based on input_type and value_type
            if setting.input_type == "number":
                # IntegerField
                if setting.value_type == "integer":
                    setattr(
                        SettingsForm, setting.key,
                        IntegerField(setting.name, validators=field_validators,
                                     description=setting.description)
                    )
                # FloatField
                elif setting.value_type == "float":
                    setattr(
                        SettingsForm, setting.key,
                        FloatField(setting.name, validators=field_validators,
                                   description=setting.description)
                    )

            # TextField
            if setting.input_type == "text":
                setattr(
                    SettingsForm, setting.key,
                    TextField(setting.name, validators=field_validators,
                              description=setting.description)
                )

            if setting.input_type == "array":
                setattr(
                    SettingsForm, setting.key,
                    TextField(setting.name, validators=field_validators,
                              description=setting.description)
                )

            # SelectField
            if setting.input_type == "choice" and "choices" in setting.extra:
                setattr(
                    SettingsForm, setting.key,
                    SelectField(setting.name, choices=setting.extra['choices'],
                                description=setting.description)
                )

            # BooleanField
            if setting.input_type == "yesno":
                setattr(
                    SettingsForm, setting.key,
                    BooleanField(setting.name, description=setting.description)
                )

        return SettingsForm

    @classmethod
    def get_all(cls):
        return cls.query.all()

    @classmethod
    def update(cls, settings, app=None):
        """Updates the current_app's config and stores the changes in the
        database.

        :param config: A dictionary with configuration items.
        """
        updated_settings = {}
        for key, value in settings.iteritems():
            setting = cls.query.filter(Setting.key == key.lower()).first()

            setting.value = value

            updated_settings[setting.key.upper()] = setting.value

            db.session.add(setting)
            db.session.commit()

        if app is not None:
            app.config.update(updated_settings)

    @classmethod
    def as_dict(cls, from_group=None, upper=False):
        """Returns the settings key and value as a dict.

        :param from_group: Returns only the settings from the group as a dict.
        :param upper: If upper is ``True``, the key will use upper-case
                      letters. Defaults to ``False``.
        """
        settings = {}
        result = None
        if from_group is not None:
            result = SettingsGroup.query.filter_by(key=from_group).\
                first_or_404()
            result = result.settings
        else:
            result = cls.query.all()

        for setting in result:
            if upper:
                settings[setting.key.upper()] = setting.value
            else:
                settings[setting.key] = setting.value

        return settings

    def save(self):
        """Saves a setting"""
        db.session.add(self)
        db.session.commit()

    def delete(self):
        """Deletes a setting"""
        db.session.delete(self)
        db.session.commit()