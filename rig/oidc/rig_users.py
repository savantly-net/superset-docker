"""The test users of the S5.2 login check. The fake provider, the fake FranOps, and the probe
read this one table. The emails differ from the rig admin's (ab_user.email is unique)."""

USERS = {
    # An org admin of the BI's organization: FranOps answers HQ.
    "hq": {"sub": "user_rigHQ0001", "email": "hq.user@rig-oidc.invalid", "name": "Hana Quinn",
           "given_name": "Hana", "family_name": "Quinn", "biRole": "HQ"},
    # A franchisee owner: FranOps answers NONE, so BI refuses the login.
    "franchisee": {"sub": "user_rigFR0002", "email": "franchisee.user@rig-oidc.invalid",
                   "name": "Frank Owner", "given_name": "Frank", "family_name": "Owner",
                   "biRole": "NONE"},
    # An HQ user for whom FranOps answers 502 (Clerk down): BI refuses the login.
    "franops_down": {"sub": "user_rigDN0003", "email": "down.user@rig-oidc.invalid",
                     "name": "Dana Down", "biRole": "HQ", "franopsStatus": 502},
    # An HQ user with no email and no names in the provider's claims: FranOps gives the email.
    "hq_bare": {"sub": "user_rigHB0004", "biRole": "HQ", "franopsEmail": "bare.hq@rig-oidc.invalid"},
    # FranOps answers after FRANOPS_TIMEOUT_SECONDS (3 on the rig): BI refuses the login.
    "franops_slow": {"sub": "user_rigSL0005", "email": "slow.user@rig-oidc.invalid", "biRole": "HQ",
                     "franopsDelay": 5},
    # The provider sends no ID token for this user: BI reads the provider's userinfo endpoint.
    "hq_no_id_token": {"sub": "user_rigNI0006", "email": "noid.hq@rig-oidc.invalid", "name": "Nia Id",
                       "biRole": "HQ", "noIdToken": True},
    # The email of the rig admin (ab_user.email is unique): BI refuses the login before FAB's
    # insert fails, because FAB's error line would hold the email.
    "hq_email_taken": {"sub": "user_rigET0007", "email": "admin@rig.invalid", "biRole": "HQ"},
    # The S5.3 MCP check: HQ at the first MCP call, then FranOps answers NONE for this user.
    "mcp_demoted": {"sub": "user_rigMD0008", "email": "demoted.hq@rig-oidc.invalid", "biRole": "HQ"},
}
