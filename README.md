# V-ZUG Home Assistant Integration

[![GitHub Release](https://img.shields.io/github/release/siku2/hass-vzug.svg?style=for-the-badge)](https://github.com/siku2/hass-vzug/releases)
[![GitHub Activity](https://img.shields.io/github/commit-activity/y/siku2/hass-vzug.svg?style=for-the-badge)](https://github.com/siku2/hass-vzug/commits/main)
[![License](https://img.shields.io/github/license/siku2/hass-vzug.svg?style=for-the-badge)](LICENSE)

[![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg?style=for-the-badge)](https://hacs.xyz/docs/faq/custom_repositories)

[![GitLocalize](https://gitlocalize.com/repo/8875/whole_project/badge.svg)](https://gitlocalize.com/repo/8875/whole_project?utm_source=badge)

_Integration to integrate with [V-ZUG](https://www.vzug.com) devices._

The following devices are known to be supported:

- AdoraDish V2000 / V4000 / V6000[^1]
- AdoraDry V2000 / V4000 / V6000
- AdoraWash V2000 / V4000 / V6000[^1]
- Combair V4000 / V6000
- CombairSteamer V6000[^1]
- CombiCooler V4000
- CombiSteam HSL / MSLQ / XSL
- CombiSteamer V6000
- CookTop V6000
- Cooler V2000
- WineCooler V4000

[Check out the discussions](https://github.com/siku2/hass-vzug/discussions) for more info.
Don't hesitate to open a new issue or a discussion if you have a V-ZUG device you would like to add.

[^1]: Owned and tested by me. These are pretty much guaranteed to work.

## Installation

Requires Home Assistant 2026.10 or newer.

1. Add this repository as a custom repository to HACS: [![Add Repository](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=siku2&repository=hass-vzug&category=integration)
2. Use HACS to install the integration.
3. Restart Home Assistant.
4. Set up the integration using the UI: [![Add Integration](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=vzug)

On Home Assistant 2026.11 or newer, HACS is built in as the Marketplace under **Settings** > **Marketplace**. The **Add Repository** button in step 1 opens the repository there, or you can add it yourself with **Add from link**.

### Setup

Adding the integration offers two ways to find an appliance:

- **Manual**: enter its IP address, host name, or base URL (for example `http://192.168.1.20`).
- **Discovery**: searches your local networks for a few seconds. Every appliance it finds shows up under **Discovered** on the **Devices & services** page, where you confirm it.

Home Assistant also discovers appliances through DHCP when they join the network, and updates the address of a configured appliance when it changes.

If the appliance is password protected, you are asked for its username and password.

### Reconfigure and reauthentication

To change the address of an appliance, open the integration under **Settings** > **Devices & services** and select **Reconfigure** on the entry. The integration keeps the stored credentials and only asks for new ones if the appliance rejects them. It refuses an address that belongs to a different appliance.

If the appliance rejects the credentials later, Home Assistant asks you to enter them again.

## How data is updated

The appliances are polled over the local network. They handle one request at a time, and the main status request wakes them up, so each kind of data has its own interval:

| Data | Interval |
| --- | --- |
| Program, status, program end | Every 30 seconds while a program runs. In standby, every 60 seconds, and only the appliance's notifications are read: this doesn't wake it. The full status is read every 5 minutes. |
| Eco counters | With the full status. |
| Settings | Every hour, whenever the appliance starts or finishes a program, and after every change made through Home Assistant. |
| Firmware | Every 6 hours. Every 5 seconds while an update installs, and every minute for up to 30 minutes after one while it is still pending. |
| Refrigerator zones | Every 30 seconds. |
| Selected program (program select) | Every 5 minutes. A program picked through Home Assistant shows up right away. |

When a new notification arrives in standby (a program finished, or a timed program started), the full status and the settings are read right away.

At startup, Home Assistant only waits for the appliance's basic information and status. Firmware, zones, programs and settings are read in the background afterwards, so some entities appear a few seconds after setup. If the appliance doesn't say yet whether it has zones or programs, it is asked again every 5 minutes.

The **Refresh Now** button reads the full status and the settings right away, waking the appliance if needed. Use it after changing something on the appliance's panel, or call it from an automation, for example when a smart plug sees the appliance draw power.

## Supported functions

Which entities you get depends on what the appliance reports.

- **Sensors**: Program, Status, Program End, and Last Notification. Program End Raw is disabled by default.
- **Eco sensors**: energy and water use of the last program, on average, and in total. They appear once the appliance reports them.
- **Settings**: the appliance's user settings, as switches, selects, numbers, buttons and sensors. Known settings are named in Home Assistant's language. Others keep the appliance's own name, in the language set on the appliance. While a program runs the appliance locks its settings: the entities still show their values, but refuse changes.
- **Refrigerators**: the fridge and the freezer are devices of their own under the appliance, each with:
  - target temperature
  - door
  - SuperCool or SuperFreeze, with its duration and the time left

  The appliance itself gets PartyCooling (with duration and time left), Holiday mode, SilentPlus, a read-only Sabbath mode sensor, and the kitchen timer.
- **Programs**: on appliances that list their programs (for example dishwashers), a select picks the program, and the **Send to appliance** button transfers it with its options. The appliance starts it once its door is closed.
- **Firmware**: an update entity that installs new firmware and shows its progress, and a **Check for Updates** button. Only on appliances whose firmware supports it.
- **Program events**: the Program event entity fires `program_started` and `program_finished`. Its `program` attribute holds the program's name.
- **Diagnostics**: see [Troubleshooting](#troubleshooting).

## Automation example

Send a notification when the dishwasher or the washing machine finishes. In the automation editor, add the **Event received** trigger, pick the appliances' Program event entities, and choose **Program finished**. In YAML:

```yaml
triggers:
  - trigger: event.received
    target:
      entity_id:
        - event.dishwasher_program
        - event.washing_machine_program
    options:
      event_type:
        - program_finished
actions:
  - action: notify.notify
    data:
      message: "Your dishes or your laundry are done."
```

Replace the entity IDs with your appliances' Program event entities, and `notify.notify` with your notification action.

## Known limitations

- Programs can't be started remotely. **Send to appliance** transfers the program, and the appliance only starts it once its door is closed.
- Settings can't be changed while a program runs.
- Changes made on the appliance's panel show up within the intervals above, or right away with **Refresh Now**.
- A program started by hand while the appliance is in standby shows up within 5 minutes.
- Some appliances switch off completely after a program. Their entities are unavailable until they are switched on again.
- Programs without a translation show their key instead of a name, for example the oven programs of an appliance set to French.
- The French translations keep the English names of V-ZUG features such as DryPlus and RinsePlus, because V-ZUG's French names aren't known.

## Troubleshooting

- **Unavailable**: the appliance doesn't answer. Check that it is switched on and connected to the network. The integration logs this once, and the entities come back when the appliance answers again. Pressing **Refresh Now** brings them back sooner.
- **Out of date**: press **Refresh Now**.
- **Diagnostics**: open the integration under **Settings** > **Devices & services**, and select **Download diagnostics** in the entry's menu. The file contains what the integration already has in memory, so downloading it doesn't contact the appliance. Credentials, the address, MAC address, serial numbers and device names are redacted.
- **Debug logs**: on the integration page, select **Enable debug logging**, reproduce the problem, then disable it again to download the log. Or in `configuration.yaml`:

  ```yaml
  logger:
    logs:
      custom_components.vzug: debug
  ```

Please report problems on the [issue tracker](https://github.com/siku2/hass-vzug/issues), with diagnostics and debug logs attached.

## Removal

1. Go to **Settings** > **Devices & services**, select **V-ZUG**, and delete each appliance's entry.
2. Remove the integration in HACS (or the Marketplace).
3. Restart Home Assistant.

## Contributions are welcome

If you want to contribute to this please read the [Contribution guidelines](CONTRIBUTING.md)

### Providing translations for other languages

If you would like to use the integration in another language, you can help out by providing the necessary translations.

[Head over to **GitLocalize** to start translating.](https://gitlocalize.com/repo/8875)

If your desired language isn't available there, just open an issue to request it.

You can also just do the translations manually in [custom_components/vzug/translations/](./custom_components/vzug/translations/) and open a pull request with the changes.
