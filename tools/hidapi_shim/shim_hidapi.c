/*
 * Process-local hidapi.dll shim for Emotiv EPOCXWIRE replay.
 *
 * Env:
 *   EPOCX_WIRE / EPOCX_WIRE_PATH  path to .epocxwire (required for useful reads)
 *   EPOCX_SPEED                   pace multiplier (default 1.0; 0 = as-fast-as-possible)
 *
 * Enumerates a single fake Emotiv device: VID 0x1234, usage 2, mfg "Emotiv",
 * serial from the wire header (AES key material).
 */
#include "hidapi.h"

#define WIN32_LEAN_AND_MEAN
#include <windows.h>

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <wchar.h>

#define SHIM_PATH "\\\\shim\\emotiv_epocx"
#define SHIM_VID 0x1234
#define SHIM_PID 0xED02
#define SHIM_USAGE 2
#define SHIM_USAGE_PAGE 0xFFA0
#define EVENT_REPORT 1
#define EVENT_EMPTY 2
#define EVENT_ERROR 3
#define EVENT_OPEN 4
#define EVENT_CLOSE 5

typedef struct {
	uint64_t rel_ns;
	uint8_t kind;
	uint8_t *payload;
	uint16_t nbytes;
} shim_event;

struct hid_device_ {
	int open;
	int nonblocking;
	size_t idx;
	int eof;
	LARGE_INTEGER t0;
	int have_t0;
};

static CRITICAL_SECTION g_lock;
static int g_lock_init = 0;
static int g_inited = 0;
static char g_serial_utf8[128] = "UD20221202006756";
static wchar_t g_serial_w[128] = L"UD20221202006756";
static wchar_t g_mfg_w[] = L"Emotiv";
static wchar_t g_product_w[] = L"EPOC X";
static char g_path[] = SHIM_PATH;
static shim_event *g_events = NULL;
static size_t g_nevents = 0;
static double g_speed = 1.0;
static wchar_t g_error[256] = L"";
static const struct hid_api_version g_version = {
	HID_API_VERSION_MAJOR, HID_API_VERSION_MINOR, HID_API_VERSION_PATCH
};

static void ensure_lock(void) {
	if (!g_lock_init) {
		InitializeCriticalSection(&g_lock);
		g_lock_init = 1;
	}
}

static void set_error(const wchar_t *msg) {
	wcsncpy(g_error, msg ? msg : L"", 255);
	g_error[255] = L'\0';
}

static void free_events(void) {
	size_t i;
	if (!g_events) {
		return;
	}
	for (i = 0; i < g_nevents; i++) {
		free(g_events[i].payload);
	}
	free(g_events);
	g_events = NULL;
	g_nevents = 0;
}

static int utf8_to_wide(const char *src, wchar_t *dst, size_t dst_n) {
	int n;
	if (!src || !dst || dst_n == 0) {
		return -1;
	}
	n = MultiByteToWideChar(CP_UTF8, 0, src, -1, dst, (int)dst_n);
	if (n <= 0) {
		n = MultiByteToWideChar(CP_ACP, 0, src, -1, dst, (int)dst_n);
	}
	return n > 0 ? 0 : -1;
}

static int append_event(uint64_t rel_ns, uint8_t kind, const uint8_t *payload, uint16_t nbytes) {
	shim_event *nbuf;
	nbuf = (shim_event *)realloc(g_events, (g_nevents + 1) * sizeof(shim_event));
	if (!nbuf) {
		return -1;
	}
	g_events = nbuf;
	g_events[g_nevents].rel_ns = rel_ns;
	g_events[g_nevents].kind = kind;
	g_events[g_nevents].nbytes = nbytes;
	g_events[g_nevents].payload = NULL;
	if (nbytes > 0) {
		g_events[g_nevents].payload = (uint8_t *)malloc(nbytes);
		if (!g_events[g_nevents].payload) {
			return -1;
		}
		memcpy(g_events[g_nevents].payload, payload, nbytes);
	}
	g_nevents++;
	return 0;
}

static const char *wire_path_from_env(void) {
	const char *p = getenv("EPOCX_WIRE");
	if (p && p[0]) {
		return p;
	}
	p = getenv("EPOCX_WIRE_PATH");
	if (p && p[0]) {
		return p;
	}
	return NULL;
}

static void load_speed(void) {
	const char *s = getenv("EPOCX_SPEED");
	char *end = NULL;
	double v;
	g_speed = 1.0;
	if (!s || !s[0]) {
		return;
	}
	v = strtod(s, &end);
	if (end != s) {
		g_speed = v;
	}
}

static int parse_header_and_events(FILE *f) {
	char line[1024];
	char magic[64] = {0};
	int version = 0;
	int in_header = 1;

	free_events();

	if (!fgets(magic, sizeof(magic), f)) {
		set_error(L"empty wire file");
		return -1;
	}
	/* strip CR/LF */
	magic[strcspn(magic, "\r\n")] = '\0';
	if (strcmp(magic, "EPOCXWIRE/1") == 0) {
		version = 1;
	} else if (strcmp(magic, "EPOCXWIRE/2") == 0) {
		version = 2;
	} else {
		set_error(L"bad EPOCXWIRE magic");
		return -1;
	}

	while (in_header && fgets(line, sizeof(line), f)) {
		line[strcspn(line, "\r\n")] = '\0';
		if (line[0] == '\0') {
			in_header = 0;
			break;
		}
		if (strncmp(line, "serial=", 7) == 0) {
			strncpy(g_serial_utf8, line + 7, sizeof(g_serial_utf8) - 1);
			g_serial_utf8[sizeof(g_serial_utf8) - 1] = '\0';
			utf8_to_wide(g_serial_utf8, g_serial_w, sizeof(g_serial_w) / sizeof(g_serial_w[0]));
		}
	}

	if (version == 1) {
		for (;;) {
			uint64_t rel_ns;
			uint16_t nbytes;
			uint8_t *buf;
			if (fread(&rel_ns, 8, 1, f) != 1) {
				break;
			}
			if (fread(&nbytes, 2, 1, f) != 1) {
				set_error(L"truncated v1 packet");
				return -1;
			}
			buf = (uint8_t *)malloc(nbytes ? nbytes : 1);
			if (!buf) {
				return -1;
			}
			if (nbytes && fread(buf, 1, nbytes, f) != nbytes) {
				free(buf);
				set_error(L"truncated v1 payload");
				return -1;
			}
			if (append_event(rel_ns, EVENT_REPORT, buf, nbytes) != 0) {
				free(buf);
				return -1;
			}
			free(buf);
		}
	} else {
		for (;;) {
			uint64_t rel_ns;
			uint8_t kind;
			if (fread(&rel_ns, 8, 1, f) != 1) {
				break;
			}
			if (fread(&kind, 1, 1, f) != 1) {
				set_error(L"truncated v2 event");
				return -1;
			}
			if (kind == EVENT_REPORT) {
				uint16_t nbytes;
				uint8_t *buf;
				if (fread(&nbytes, 2, 1, f) != 1) {
					set_error(L"truncated REPORT len");
					return -1;
				}
				buf = (uint8_t *)malloc(nbytes ? nbytes : 1);
				if (!buf) {
					return -1;
				}
				if (nbytes && fread(buf, 1, nbytes, f) != nbytes) {
					free(buf);
					set_error(L"truncated REPORT payload");
					return -1;
				}
				if (append_event(rel_ns, kind, buf, nbytes) != 0) {
					free(buf);
					return -1;
				}
				free(buf);
			} else if (kind == EVENT_ERROR) {
				uint16_t msg_len;
				if (fread(&msg_len, 2, 1, f) != 1) {
					return -1;
				}
				if (msg_len) {
					fseek(f, (long)msg_len, SEEK_CUR);
				}
				if (append_event(rel_ns, kind, NULL, 0) != 0) {
					return -1;
				}
			} else if (kind == EVENT_EMPTY || kind == EVENT_OPEN || kind == EVENT_CLOSE) {
				if (append_event(rel_ns, kind, NULL, 0) != 0) {
					return -1;
				}
			} else {
				set_error(L"unknown event kind");
				return -1;
			}
		}
	}
	return 0;
}

static int load_wire_file(void) {
	const char *path = wire_path_from_env();
	FILE *f;
	int rc;
	load_speed();
	if (!path) {
		/* Still enumerate a device; reads return empty/EOF immediately. */
		free_events();
		set_error(L"EPOCX_WIRE not set");
		return 0;
	}
	f = fopen(path, "rb");
	if (!f) {
		set_error(L"failed to open EPOCX_WIRE");
		return -1;
	}
	rc = parse_header_and_events(f);
	fclose(f);
	return rc;
}

static void pace(hid_device *dev, uint64_t rel_ns) {
	LARGE_INTEGER freq, now;
	double target_s, elapsed_s, delay_s;
	if (g_speed <= 0.0) {
		return;
	}
	QueryPerformanceFrequency(&freq);
	if (!dev->have_t0) {
		QueryPerformanceCounter(&dev->t0);
		dev->have_t0 = 1;
	}
	target_s = ((double)rel_ns * 1e-9) / g_speed;
	QueryPerformanceCounter(&now);
	elapsed_s = (double)(now.QuadPart - dev->t0.QuadPart) / (double)freq.QuadPart;
	delay_s = target_s - elapsed_s;
	if (delay_s > 0.0) {
		DWORD ms = (DWORD)(delay_s * 1000.0);
		if (ms > 0) {
			Sleep(ms);
		}
	}
}

static wchar_t *wcsdup_local(const wchar_t *s) {
	size_t n;
	wchar_t *o;
	if (!s) {
		return NULL;
	}
	n = wcslen(s) + 1;
	o = (wchar_t *)malloc(n * sizeof(wchar_t));
	if (!o) {
		return NULL;
	}
	memcpy(o, s, n * sizeof(wchar_t));
	return o;
}

static char *strdup_local(const char *s) {
	size_t n;
	char *o;
	if (!s) {
		return NULL;
	}
	n = strlen(s) + 1;
	o = (char *)malloc(n);
	if (!o) {
		return NULL;
	}
	memcpy(o, s, n);
	return o;
}

static struct hid_device_info *make_info(void) {
	struct hid_device_info *info = (struct hid_device_info *)calloc(1, sizeof(*info));
	if (!info) {
		return NULL;
	}
	info->path = strdup_local(g_path);
	info->vendor_id = SHIM_VID;
	info->product_id = SHIM_PID;
	info->serial_number = wcsdup_local(g_serial_w);
	info->release_number = 0x0100;
	info->manufacturer_string = wcsdup_local(g_mfg_w);
	info->product_string = wcsdup_local(g_product_w);
	info->usage_page = SHIM_USAGE_PAGE;
	info->usage = SHIM_USAGE;
	info->interface_number = 1;
	info->next = NULL;
	info->bus_type = HID_API_BUS_USB;
	return info;
}

HID_API_EXPORT int HID_API_CALL hid_init(void) {
	ensure_lock();
	EnterCriticalSection(&g_lock);
	if (!g_inited) {
		if (load_wire_file() != 0) {
			/* keep going with empty event list if file missing after set */
		}
		g_inited = 1;
	}
	LeaveCriticalSection(&g_lock);
	return 0;
}

HID_API_EXPORT int HID_API_CALL hid_exit(void) {
	ensure_lock();
	EnterCriticalSection(&g_lock);
	free_events();
	g_inited = 0;
	LeaveCriticalSection(&g_lock);
	return 0;
}

HID_API_EXPORT struct hid_device_info * HID_API_CALL hid_enumerate(unsigned short vendor_id, unsigned short product_id) {
	struct hid_device_info *info;
	hid_init();
	if (vendor_id != 0 && vendor_id != SHIM_VID) {
		return NULL;
	}
	if (product_id != 0 && product_id != SHIM_PID) {
		return NULL;
	}
	info = make_info();
	return info;
}

HID_API_EXPORT void HID_API_CALL hid_free_enumeration(struct hid_device_info *devs) {
	while (devs) {
		struct hid_device_info *next = devs->next;
		free(devs->path);
		free(devs->serial_number);
		free(devs->manufacturer_string);
		free(devs->product_string);
		free(devs);
		devs = next;
	}
}

HID_API_EXPORT hid_device * HID_API_CALL hid_open(unsigned short vendor_id, unsigned short product_id, const wchar_t *serial_number) {
	(void)serial_number;
	if (vendor_id != 0 && vendor_id != SHIM_VID) {
		return NULL;
	}
	if (product_id != 0 && product_id != SHIM_PID) {
		return NULL;
	}
	return hid_open_path(g_path);
}

HID_API_EXPORT hid_device * HID_API_CALL hid_open_path(const char *path) {
	hid_device *dev;
	hid_init();
	if (!path || (strcmp(path, g_path) != 0 && strcmp(path, "shim") != 0)) {
		/* Accept any path so callers that rewrite paths still work when only one device exists. */
	}
	dev = (hid_device *)calloc(1, sizeof(*dev));
	if (!dev) {
		return NULL;
	}
	dev->open = 1;
	dev->nonblocking = 0;
	dev->idx = 0;
	dev->eof = 0;
	dev->have_t0 = 0;
	return dev;
}

static int read_next(hid_device *dev, unsigned char *data, size_t length, int milliseconds) {
	(void)milliseconds;
	if (!dev || !dev->open) {
		return -1;
	}
	ensure_lock();
	EnterCriticalSection(&g_lock);
	while (dev->idx < g_nevents) {
		shim_event *ev = &g_events[dev->idx++];
		LeaveCriticalSection(&g_lock);
		pace(dev, ev->rel_ns);
		if (ev->kind == EVENT_OPEN || ev->kind == EVENT_CLOSE || ev->kind == EVENT_ERROR) {
			EnterCriticalSection(&g_lock);
			continue;
		}
		if (ev->kind == EVENT_EMPTY) {
			return 0;
		}
		if (ev->kind == EVENT_REPORT) {
			size_t n = ev->nbytes;
			if (n > length) {
				n = length;
			}
			if (n && data) {
				memcpy(data, ev->payload, n);
			}
			return (int)n;
		}
		EnterCriticalSection(&g_lock);
	}
	dev->eof = 1;
	LeaveCriticalSection(&g_lock);
	if (dev->nonblocking) {
		return 0;
	}
	/* Blocking EOF: brief sleep then 0 so callers can detect stall/disconnect. */
	Sleep(milliseconds > 0 ? (DWORD)milliseconds : 1);
	return 0;
}

HID_API_EXPORT int HID_API_CALL hid_read_timeout(hid_device *dev, unsigned char *data, size_t length, int milliseconds) {
	return read_next(dev, data, length, milliseconds);
}

HID_API_EXPORT int HID_API_CALL hid_read(hid_device *dev, unsigned char *data, size_t length) {
	return read_next(dev, data, length, -1);
}

HID_API_EXPORT int HID_API_CALL hid_write(hid_device *dev, const unsigned char *data, size_t length) {
	(void)dev;
	(void)data;
	return (int)length;
}

HID_API_EXPORT int HID_API_CALL hid_set_nonblocking(hid_device *dev, int nonblock) {
	if (!dev) {
		return -1;
	}
	dev->nonblocking = nonblock ? 1 : 0;
	return 0;
}

HID_API_EXPORT int HID_API_CALL hid_send_feature_report(hid_device *dev, const unsigned char *data, size_t length) {
	(void)dev;
	(void)data;
	return (int)length;
}

HID_API_EXPORT int HID_API_CALL hid_get_feature_report(hid_device *dev, unsigned char *data, size_t length) {
	(void)dev;
	(void)data;
	(void)length;
	return -1;
}

HID_API_EXPORT int HID_API_CALL hid_get_input_report(hid_device *dev, unsigned char *data, size_t length) {
	(void)dev;
	(void)data;
	(void)length;
	return -1;
}

HID_API_EXPORT void HID_API_CALL hid_close(hid_device *dev) {
	if (dev) {
		dev->open = 0;
		free(dev);
	}
}

static int copy_wstr(wchar_t *dst, size_t maxlen, const wchar_t *src) {
	if (!dst || maxlen == 0) {
		return -1;
	}
	wcsncpy(dst, src ? src : L"", maxlen - 1);
	dst[maxlen - 1] = L'\0';
	return 0;
}

HID_API_EXPORT int HID_API_CALL hid_get_manufacturer_string(hid_device *dev, wchar_t *string, size_t maxlen) {
	(void)dev;
	return copy_wstr(string, maxlen, g_mfg_w);
}

HID_API_EXPORT int HID_API_CALL hid_get_product_string(hid_device *dev, wchar_t *string, size_t maxlen) {
	(void)dev;
	return copy_wstr(string, maxlen, g_product_w);
}

HID_API_EXPORT int HID_API_CALL hid_get_serial_number_string(hid_device *dev, wchar_t *string, size_t maxlen) {
	(void)dev;
	return copy_wstr(string, maxlen, g_serial_w);
}

HID_API_EXPORT int HID_API_CALL hid_get_indexed_string(hid_device *dev, int string_index, wchar_t *string, size_t maxlen) {
	(void)dev;
	(void)string_index;
	return copy_wstr(string, maxlen, L"");
}

HID_API_EXPORT int HID_API_CALL hid_get_report_descriptor(hid_device *dev, unsigned char *buf, size_t buf_size) {
	(void)dev;
	(void)buf;
	(void)buf_size;
	return -1;
}

HID_API_EXPORT struct hid_device_info * HID_API_CALL hid_get_device_info(hid_device *dev) {
	(void)dev;
	return make_info();
}

HID_API_EXPORT const wchar_t * HID_API_CALL hid_error(hid_device *dev) {
	(void)dev;
	return g_error;
}

HID_API_EXPORT const struct hid_api_version * HID_API_CALL hid_version(void) {
	return &g_version;
}

HID_API_EXPORT const char * HID_API_CALL hid_version_str(void) {
	return "0.15.0-emotiv-shim";
}

BOOL WINAPI DllMain(HINSTANCE hinst, DWORD reason, LPVOID reserved) {
	(void)hinst;
	(void)reserved;
	if (reason == DLL_PROCESS_ATTACH) {
		ensure_lock();
	}
	return TRUE;
}
