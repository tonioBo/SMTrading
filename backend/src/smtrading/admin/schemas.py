from typing import Annotated

from pydantic import BaseModel, Field

ShortStr = Annotated[str, Field(max_length=256)]
TotpCode = Annotated[str, Field(max_length=16)]


class LoginIn(BaseModel):
    email: ShortStr
    password: ShortStr
    totp: TotpCode | None = None


class TotpCodeIn(BaseModel):
    code: TotpCode


class PasswordChangeIn(BaseModel):
    current: ShortStr
    new: ShortStr


class AdminOut(BaseModel):
    email: str
    totp_enabled: bool


class TotpSetupOut(BaseModel):
    secret: str
    otpauth_uri: str
