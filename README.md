# g0v0-client-versions

This repository provides a tool to generate version hashes for different clients of the osu! game. The version hashes are used to identify specific versions of the game across various platforms.

The latest generated version hashes can be found in the [version_list.json](version_list.json) file.

## Client schema Definition

The client definitions are stored in JSON files that follow a specific schema. The schema defines the structure and required fields for each client entry. The schema file is located at [schemas/clients.schema.json](schemas/clients.schema.json).

### Properties

- `name` (string): The name of the client.
- `description` (string): A description of the client.
- `owner` (string): The owner of the repository.
- `repo` (string): The repository name.
- `files` (object): Mapping of platform to file details.
  - `asset_name` (string): The filename in the release assets.
  - `internal_name` (string): The internal path to the file (e.g. dll).
  - `type` (string): The type of the file (e.g. 'zip', 'exe', 'appimage'). Default is 'zip'.
- `count` (integer): Number of lazer versions supported. Minimum 1. Default 1.
- `support_android` (boolean): Whether the client supports Android platform. Default is true.
- `support_ios` (boolean): Whether the client supports iOS platform. Default is true.

## Generate for your own client by adding a new community clients definition

You can add your clients definition by creating a new JSON file in the `community_clients/` directory and send a pull request to this repository.

1. Fork this repository.

2. Create a new JSON file in the `community_clients/` directory following the same schema as `default_clients.json`.

3. Push your changes to your forked repository and create a pull request to this repository.

4. Once your pull request is merged, you can find the generated `{your_client}.json` at `community_versions/`. Add the generated URL to your g0v0-server configuration.

```env
CLIENT_VERSION_URLS='[
    "https://raw.githubusercontent.com/GooGuTeam/g0v0-client-versions/main/community_versions/your_client.json",
    "https://raw.githubusercontent.com/GooGuTeam/g0v0-client-versions/main/version_list.json"
]'
```

## Generate for your own client by replacing `default_clients.json`

You can also modify the `default_clients.json` file to include your own client details. The CI workflow will automatically generate the version hashes for your client whenever you push changes to the repository.

1. Fork this repository.

2. Modify the `default_clients.json` file to include your client details. You can specify the repository, description, and files for different platforms.

3. Push your changes to your forked repository. The CI workflow will automatically run the version hash generator and update the `version_list.json` file with the new hashes.

4. Add the generated `version_list.json` url to your g0v0-server configuration to enable your client to use the generated version hashes.

```env
CLIENT_VERSION_URLS='[
    "https://raw.githubusercontent.com/your-username/your-forked-repo/main/version_list.json",
    "https://raw.githubusercontent.com/GooGuTeam/g0v0-client-versions/main/version_list.json"
]'
```

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.
