from pydantic import BaseModel


class LoginIn(BaseModel):
    email: str
    password: str
    totp: str | None = None


class TotpCodeIn(BaseModel):
    code: str


class PasswordChangeIn(BaseModel):
    current: str
    new: str


class AdminOut(BaseModel):
    email: str
    totp_enabled: bool


class TotpSetupOut(BaseModel):
    secret: str
    otpauth_uri: str
