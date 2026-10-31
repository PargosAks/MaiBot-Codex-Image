"""Register frozen reference bytes once; recover uncertain receipts read-only."""

import hashlib
import json

from .bridge import BridgeError
from .policy import StoreError
from .recovery import ReadOnlyRecovery


class ReferenceInputs:
    def __init__(self, store, space_id, scope, bridge, *, binding_id):
        self.store, self.space_id, self.scope = store, space_id, scope
        self.bridge, self.binding_id = bridge, binding_id

    async def _db(self, name, *args, **kwargs):
        return await ReadOnlyRecovery._db(
            getattr(self.store, name), self.space_id, self.scope, *args, **kwargs,
        )

    async def _authority(self, epoch):
        policy = await self._db("validate_reference_authorization", expected_auth_version=epoch)
        space = await self._db("get_space")
        if space["binding_id"] != self.binding_id:
            raise StoreError("BINDING_MISMATCH", "Reference service binding changed")
        return policy

    async def reconcile(self, asset_id):
        """An administrator may recover an original receipt after revocation.

        This path never imports bytes, changes request identity or authorizes a
        subsequent render. A missing receipt remains unknown.
        """
        space = await self._db("get_space")
        registration = await self._db("get_asset_registration", asset_id)
        if (space["binding_id"] != self.binding_id or registration is None
                or registration["binding_id"] != self.binding_id):
            raise StoreError("BINDING_MISMATCH", "No registration belongs to this service binding")
        if registration["state"] == "succeeded":
            return registration
        if registration["state"] not in {"submitting", "unknown"}:
            raise StoreError("INVALID_STATE", "Only attempted registrations can be reconciled")
        found = await self.bridge.asset_lookup(
            json.loads(self.scope.snapshot()), registration["request_id"],
            expected_sha256=registration["sha256"], expected_mime=registration["mime"],
            expected_size=registration["size_bytes"],
        )
        if found.get("found") is not True:
            raise StoreError("ASSET_REGISTRATION_UNKNOWN", "Original asset receipt is still unavailable")
        return await self._db("complete_asset_registration", asset_id, found["asset"])

    @staticmethod
    def _limits(health):
        if not isinstance(health, dict) or "reference_inputs_v1" not in health.get("capabilities", []):
            raise BridgeError("REFERENCE_INPUTS_UNSUPPORTED", kind="capability")
        limits = health.get("reference_limits")
        required = {"max_image_bytes", "max_references", "max_total_bytes", "max_asset_bytes"}
        if (not isinstance(limits, dict) or not required <= limits.keys()
                or any(type(limits[key]) is not int or limits[key] <= 0 for key in required)):
            raise BridgeError("INVALID_RESPONSE", kind="protocol")
        return limits

    async def prepare(self, references, *, expected_auth_version, source_size=0):
        if (type(references) is not list or not 1 <= len(references) <= 3
                or type(source_size) is not int or not 0 <= source_size <= 8 * 1024 * 1024):
            raise StoreError("INVALID_ARGUMENT", "Reference inputs exceed local limits")
        async def authority():
            policy = await self._authority(expected_auth_version)
            if any((ref.get("policy_version"), ref.get("consent_ref")) !=
                   (policy["version"], policy["consent_ref"]) for ref in references):
                raise StoreError("REFERENCE_AUTH_CHANGED", "Frozen image delegation no longer matches")
            return policy

        await authority()
        scope = json.loads(self.scope.snapshot())
        limits = self._limits(await self.bridge.call("health", scope, {}))
        if len(references) > limits["max_references"]:
            raise StoreError("ASSET_TOO_LARGE", "Service reference count limit is smaller")
        # Verify the entire frozen set before importing the first image.
        images, total = [], source_size
        for reference in references:
            if (type(reference) is not dict or reference.get("usage") != "image_input"
                    or reference.get("role") not in {"character", "style", "composition", "palette"}):
                raise StoreError("INVALID_ARGUMENT", "Only explicit frozen image references can be registered")
            image = await self._db("get_asset_image", reference["asset_id"])
            data = image.get("data")
            if (not isinstance(data, bytes) or not 0 < len(data) <= min(8 * 1024 * 1024, limits["max_image_bytes"])
                    or image.get("mime") != reference["mime"]
                    or image.get("sha256") != reference["sha256"]
                    or hashlib.sha256(data).hexdigest() != reference["sha256"]):
                raise StoreError("ASSET_HASH_MISMATCH", "Reference bytes differ from the frozen plan")
            total += len(data)
            images.append(image)
        if total > min(32 * 1024 * 1024, limits["max_total_bytes"]):
            raise StoreError("ASSET_TOO_LARGE", "Combined source and reference bytes exceed the limit")
        result = []
        for reference, image in zip(references, images):
            await authority()
            registration = await self._db(
                "reserve_asset_registration", asset_id=reference["asset_id"],
                expected_auth_version=expected_auth_version,
            )
            if registration["binding_id"] != self.binding_id:
                raise StoreError("BINDING_MISMATCH", "Registration belongs to another service")
            if registration["state"] == "reserved":
                registration = await self._db(
                    "claim_asset_registration", reference["asset_id"],
                    expected_auth_version=expected_auth_version,
                )

                async def before_dispatch():
                    await authority()
                    current = await self._db("get_asset_registration", reference["asset_id"])
                    if (current["state"] != "submitting" or current["request_id"] != registration["request_id"]
                            or current["auth_version"] != expected_auth_version):
                        raise StoreError("AUTH_CHANGED", "Registration dispatch claim is no longer valid")

                try:
                    receipt = await self.bridge.asset_register(
                        scope, registration["request_id"], data=image["data"], mime=image["mime"],
                        expected_sha256=image["sha256"], before_dispatch=before_dispatch,
                    )
                    registration = await self._db(
                        "complete_asset_registration", reference["asset_id"], receipt,
                    )
                except BaseException as exc:
                    await self._db("mark_asset_registration_unknown", reference["asset_id"],
                                   reason=getattr(exc, "code", "ASSET_REGISTRATION_UNKNOWN"))
                    raise
            elif registration["state"] in {"submitting", "unknown"}:
                registration = await self.reconcile(reference["asset_id"])
            if registration["state"] != "succeeded":
                raise StoreError("ASSET_REGISTRATION_UNKNOWN", "Asset registration has no confirmed receipt")
            receipt = registration["receipt"]
            if (receipt.get("sha256") != image["sha256"] or receipt.get("mime") != image["mime"]
                    or receipt.get("size_bytes") != len(image["data"])):
                raise StoreError("ASSET_HASH_MISMATCH", "Registered asset receipt differs from the frozen image")
            result.append({"asset_id": receipt["asset_id"], "sha256": image["sha256"], "role": reference["role"]})
        await authority()
        return result
