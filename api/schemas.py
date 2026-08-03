from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class WhatsAppMessageRequest(BaseModel):
    contact_phone: str
    message: str


class WhatsAppTestRequest(BaseModel):
    contact_phone: str
    message: str = "hai"
    mode: str = Field(
        default="auto",
        description=(
            "Send mode: `auto` (text then template fallback), `text`, `template` (hello_world), "
            "or `business-card` (cardsync_contact_saved from WHATSAPP_*_TEMPLATE_NAME env)."
        ),
    )


class WhatsAppCardReceivedRequest(BaseModel):
    contact_phone: str = Field(
        ...,
        description="Recipient phone number (E.164 or local 10-digit Indian number).",
        examples=["6309248193"],
    )
    full_name: str = Field(
        default="Yogesh VR",
        description="Name for card_final_ula {{1}} (Hi {{1}}). First word is used.",
    )
    event_name: str = Field(
        default="TTF Kolkata 2026",
        description="Event/place for card_final_ula {{2}} (meeting you at {{2}}).",
    )


class WhatsAppChatReplyRegisterRequest(BaseModel):
    """Register a scanned contact for auto-reply after they message via wa.me QR."""

    fullName: str = ""
    firstName: str = ""
    lastName: str = ""
    designation: str = ""
    company: str = ""
    phone: str = ""
    secondaryPhone: str = ""
    email: str = ""
    secondaryEmail: str = ""
    website: str = ""
    secondaryWebsite: str = ""
    address: str = ""
    secondaryAddress: str = ""


class EmailMessageRequest(BaseModel):
    contact_email: str
    message: str


class EmailTestRequest(BaseModel):
    contact_email: str = Field(..., description="Email parsed from a scanned contact (simulated).")
    test_override: str = Field(
        ...,
        description=(
            "Optional inbox that receives the mail instead of contact_email. "
            "Use empty string to send to contact_email."
        ),
    )


class DuplicateCheckRequest(BaseModel):
    fullName: str = ""
    company: str = ""
    phone: str = ""
    countryCode: str = ""
    email: str = ""


class ContactUpdateRequest(BaseModel):
    contact: dict


class LocalContactBody(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "fullName": "Sandeep Saliganti",
                    "firstName": "Sandeep",
                    "lastName": "Saliganti",
                    "designation": "Developer",
                    "company": "CardSync Demo",
                    "phone": "9884993074",
                    "countryCode": "+91",
                    "countryName": "India",
                    "email": "saligantisandeepzzz6@gmail.com",
                    "website": "https://cardsync.ai",
                    "eventName": "Mall Opening",
                    "eventDay": "Day 1",
                    "notes": "Met at booth — follow up next week.",
                    "connectionMode": "online",
                    "skipWhatsApp": True,
                    "skipEmail": False,
                }
            ]
        }
    )

    fullName: str
    firstName: str = ""
    lastName: str = ""
    designation: str = ""
    company: str = ""
    countryCode: str = Field(default="", description="Dial code only, e.g. +91.")
    countryName: str = Field(default="", description="Country display name, e.g. India.")
    phone: str = ""
    secondaryPhone: str = ""
    email: str = ""
    secondaryEmail: str = ""
    website: str = ""
    secondaryWebsite: str = ""
    address: str = ""
    secondaryAddress: str = ""
    socialLinks: str = ""
    gstNumber: str = ""
    notes: str = Field(
        default="",
        description="User-written notes only (not OCR). Max 2000 characters.",
        max_length=2000,
    )
    eventName: str = Field(
        default="",
        description="Event where the card was collected.",
    )
    eventDay: str = Field(
        default="Day 1",
        description="Exhibition/event day label used for Google Sheets worksheets.",
    )
    eventId: str | None = None
    cardImageBase64: str | None = None
    syncStatus: str = "synced"
    connectionMode: str = "online"
    skipWhatsApp: bool = False
    skipEmail: bool = False
    # Scan metadata — not persisted in PostgreSQL; forwarded to the
    # Google Sheets reporting sync when configured.
    ocrEngine: str = ""
    ocrConfidence: float | None = None
    captureSource: str = ""


class SyncStatusBody(BaseModel):
    syncStatus: str


class SyncOutreachOptions(BaseModel):
    skipWhatsApp: bool = False
    skipEmail: bool = False


class WipeAllDataBody(BaseModel):
    confirm: bool = False


# ═══════════════════════════════════════════════════════════════════════════
# Auth / RBAC schemas
# ═══════════════════════════════════════════════════════════════════════════


class LoginRequest(BaseModel):
    identifier: str = Field(..., description="Email or username")
    password: str = Field(..., min_length=1, description="Account password")


class RefreshTokenRequest(BaseModel):
    refresh_token: str = Field(..., description="Refresh token from login response")


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    email: EmailStr
    otp: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")
    password: str = Field(min_length=8)


class VerifyEmailRequest(BaseModel):
    token: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = 900


class UserInfo(BaseModel):
    id: str
    email: str
    first_name: str = ""
    last_name: str = ""
    username: str = ""
    phone: str = ""
    role: str = ""
    company_id: str | None = None
    admin_id: str | None = None
    is_active: bool = True
    is_verified: bool = False
    permissions: list[str] = []


class CreateUserRequest(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{
        "first_name": "John", "last_name": "Doe", "email": "john@example.com",
        "username": "johndoe", "password": "Password@123", "role": "USER", "phone": "+1234567890",
    }]})

    first_name: str = Field(..., min_length=1)
    last_name: str = Field(..., min_length=1)
    email: EmailStr
    username: str = Field(..., min_length=3)
    password: str = Field(..., min_length=8)
    role: str = Field(default="USER", description="SUPER_ADMIN, ADMIN, or USER")
    company_id: str | None = None
    phone: str = ""


class UpdateUserRequest(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    phone: str | None = None
    role: str | None = None
    is_active: bool | None = None


class UserStatusRequest(BaseModel):
    is_active: bool


class AdminResetPasswordRequest(BaseModel):
    new_password: str = Field(..., min_length=8)


class CreateCompanyRequest(BaseModel):
    """Invite an Admin for a new company. Admin sets their own password via the invite link."""

    model_config = ConfigDict(json_schema_extra={"examples": [{
        "company_name": "Acme Corp", "company_code": "ACME",
        "admin_email": "admin@acme.com",
    }]})

    company_name: str = Field(..., min_length=1)
    company_code: str = Field(..., min_length=2)
    admin_email: EmailStr
    address: str = ""
    phone: str = ""
    email: str = ""
    website: str = ""


class UpdateCompanyRequest(BaseModel):
    company_name: str | None = None
    address: str | None = None
    phone: str | None = None
    email: str | None = None
    website: str | None = None
    status: str | None = None


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(..., min_length=8)


class ChangeEmailRequest(BaseModel):
    new_email: EmailStr


class UpdateProfileRequest(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    phone: str | None = None
