// The xDrip+ sync key lives in the GNOME Keyring (libsecret), never in GSettings or on a command line.
import Secret from 'gi://Secret';

export const KEY_SCHEMA = new Secret.Schema('org.local.xdrip-topbar.SyncKey', Secret.SchemaFlags.NONE, {
    purpose: Secret.SchemaAttributeType.STRING,
});
export const KEY_ATTRS = {purpose: 'xdrip-sync-key'};
export const KEY_LABEL = 'xDrip+ sync key (xDrip+ Top Bar extension)';

/** callback(key or null) */
export function lookupKey(callback) {
    Secret.password_lookup(KEY_SCHEMA, KEY_ATTRS, null, (src, res) => {
        let key = null;
        try {
            key = Secret.password_lookup_finish(res);
        } catch (e) {
            key = null;
        }
        callback(key || null);
    });
}

export function storeKey(key, callback) {
    Secret.password_store(KEY_SCHEMA, KEY_ATTRS, Secret.COLLECTION_DEFAULT, KEY_LABEL, key, null, (src, res) => {
        let ok = false;
        try {
            ok = Secret.password_store_finish(res);
        } catch (e) {
            ok = false;
        }
        callback(ok);
    });
}

export function clearKey(callback) {
    Secret.password_clear(KEY_SCHEMA, KEY_ATTRS, null, (src, res) => {
        try {
            Secret.password_clear_finish(res);
        } catch (e) { /* nothing stored */ }
        callback();
    });
}
