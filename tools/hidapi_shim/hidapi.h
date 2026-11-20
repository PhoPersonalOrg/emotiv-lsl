/* Minimal hidapi ABI for the Emotiv EPOCXWIRE shim (subset of libusb/hidapi). */
#ifndef HIDAPI_SHIM_HIDAPI_H__
#define HIDAPI_SHIM_HIDAPI_H__

#include <stddef.h>
#include <wchar.h>

#ifdef _WIN32
#ifndef HID_API_NO_EXPORT_DEFINE
#define HID_API_EXPORT __declspec(dllexport)
#endif
#endif
#ifndef HID_API_EXPORT
#define HID_API_EXPORT
#endif
#define HID_API_CALL
#define HID_API_EXPORT_CALL HID_API_EXPORT HID_API_CALL

#define HID_API_VERSION_MAJOR 0
#define HID_API_VERSION_MINOR 15
#define HID_API_VERSION_PATCH 0
#define HID_API_MAKE_VERSION(mj, mn, p) (((mj) << 24) | ((mn) << 8) | (p))
#define HID_API_VERSION HID_API_MAKE_VERSION(HID_API_VERSION_MAJOR, HID_API_VERSION_MINOR, HID_API_VERSION_PATCH)

#ifdef __cplusplus
extern "C" {
#endif

struct hid_api_version {
	int major;
	int minor;
	int patch;
};

struct hid_device_;
typedef struct hid_device_ hid_device;

typedef enum {
	HID_API_BUS_UNKNOWN = 0x00,
	HID_API_BUS_USB = 0x01,
	HID_API_BUS_BLUETOOTH = 0x02,
	HID_API_BUS_I2C = 0x03,
	HID_API_BUS_SPI = 0x04
} hid_bus_type;

struct hid_device_info {
	char *path;
	unsigned short vendor_id;
	unsigned short product_id;
	wchar_t *serial_number;
	unsigned short release_number;
	wchar_t *manufacturer_string;
	wchar_t *product_string;
	unsigned short usage_page;
	unsigned short usage;
	int interface_number;
	struct hid_device_info *next;
	hid_bus_type bus_type;
};

HID_API_EXPORT int HID_API_CALL hid_init(void);
HID_API_EXPORT int HID_API_CALL hid_exit(void);
HID_API_EXPORT struct hid_device_info * HID_API_CALL hid_enumerate(unsigned short vendor_id, unsigned short product_id);
HID_API_EXPORT void HID_API_CALL hid_free_enumeration(struct hid_device_info *devs);
HID_API_EXPORT hid_device * HID_API_CALL hid_open(unsigned short vendor_id, unsigned short product_id, const wchar_t *serial_number);
HID_API_EXPORT hid_device * HID_API_CALL hid_open_path(const char *path);
HID_API_EXPORT int HID_API_CALL hid_write(hid_device *dev, const unsigned char *data, size_t length);
HID_API_EXPORT int HID_API_CALL hid_read_timeout(hid_device *dev, unsigned char *data, size_t length, int milliseconds);
HID_API_EXPORT int HID_API_CALL hid_read(hid_device *dev, unsigned char *data, size_t length);
HID_API_EXPORT int HID_API_CALL hid_set_nonblocking(hid_device *dev, int nonblock);
HID_API_EXPORT int HID_API_CALL hid_send_feature_report(hid_device *dev, const unsigned char *data, size_t length);
HID_API_EXPORT int HID_API_CALL hid_get_feature_report(hid_device *dev, unsigned char *data, size_t length);
HID_API_EXPORT int HID_API_CALL hid_get_input_report(hid_device *dev, unsigned char *data, size_t length);
HID_API_EXPORT void HID_API_CALL hid_close(hid_device *dev);
HID_API_EXPORT int HID_API_CALL hid_get_manufacturer_string(hid_device *dev, wchar_t *string, size_t maxlen);
HID_API_EXPORT int HID_API_CALL hid_get_product_string(hid_device *dev, wchar_t *string, size_t maxlen);
HID_API_EXPORT int HID_API_CALL hid_get_serial_number_string(hid_device *dev, wchar_t *string, size_t maxlen);
HID_API_EXPORT int HID_API_CALL hid_get_indexed_string(hid_device *dev, int string_index, wchar_t *string, size_t maxlen);
HID_API_EXPORT int HID_API_CALL hid_get_report_descriptor(hid_device *dev, unsigned char *buf, size_t buf_size);
HID_API_EXPORT struct hid_device_info * HID_API_CALL hid_get_device_info(hid_device *dev);
HID_API_EXPORT const wchar_t * HID_API_CALL hid_error(hid_device *dev);
HID_API_EXPORT const struct hid_api_version * HID_API_CALL hid_version(void);
HID_API_EXPORT const char * HID_API_CALL hid_version_str(void);

#ifdef __cplusplus
}
#endif

#endif /* HIDAPI_SHIM_HIDAPI_H__ */
