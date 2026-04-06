from pydantic import BaseModel, ConfigDict


def to_camel(string: str) -> str:
    """Convert snake_case to camelCase for JSON serialization."""
    parts = string.split("_")
    return parts[0] + "".join(word.capitalize() for word in parts[1:])


class AppBaseSchema(BaseModel):
    """
    Base Pydantic schema for all API input/output models.

    - Uses camelCase aliases for JSON (matches JS/TS conventions).
    - Allows population by both field name and alias.
    - Supports construction from ORM model instances (from_attributes).
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
    )
