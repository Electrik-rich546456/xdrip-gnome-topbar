// The xDrip sync key (and the Nightscout token / API secret) live in the GNOME Keyring (libsecret), never in GSettings or on a command line.
import Secret from 'gi://Secret';

export const KEY_SCHEMA = new Secret.Schema('org.local.xdrip-topbar.SyncKey', Secret.SchemaFlags.NONE, {
    purpose: Secret.SchemaAttributeType.STRING,
});
export const KEY_ATTRS = {purpose: 'xdrip-sync-key'};
export const KEY_LABEL = 'xDrip sync key (xDrip Top Bar extension)';
// Nightscout uses the same schema with its own attribute value (added later, nothing above changes)
export const NS_ATTRS = {purpose: 'nightscout-secret'};
export const NS_LABEL = 'Nightscout token or API secret (xDrip Top Bar extension)';

/** callback(key or null) */
export function lookupKey(callback, attrs = KEY_ATTRS) {
    Secret.password_lookup(KEY_SCHEMA, attrs, null, (src, res) => {
        let key = null;
        try {
            key = Secret.password_lookup_finish(res);
        } catch (e) {
            key = null;
        }
        callback(key || null);
    });
}

export function storeKey(key, callback, attrs = KEY_ATTRS, label = KEY_LABEL) {
    Secret.password_store(KEY_SCHEMA, attrs, Secret.COLLECTION_DEFAULT, label, key, null, (src, res) => {
        let ok = false;
        try {
            ok = Secret.password_store_finish(res);
        } catch (e) {
            ok = false;
        }
        callback(ok);
    });
}

export function clearKey(callback, attrs = KEY_ATTRS) {
    Secret.password_clear(KEY_SCHEMA, attrs, null, (src, res) => {
        try {
            Secret.password_clear_finish(res);
        } catch (e) { /* nothing stored */ }
        callback();
    });
}

export const lookupNsSecret = cb => lookupKey(cb, NS_ATTRS);
export const storeNsSecret = (v, cb) => storeKey(v, cb, NS_ATTRS, NS_LABEL);
export const clearNsSecret = cb => clearKey(cb, NS_ATTRS);
