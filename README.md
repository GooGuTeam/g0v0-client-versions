# g0v0-client-versions

This repository provides a tool to generate version hashes for different clients of the osu! game. The version hashes are used to identify specific versions of the game across various platforms.

The latest generated version hashes can be found in the [version_list.json](version_list.json) file.

## Generate for your own client

1. Fork this repository.

2. Modify the `clients.json` file to include your client details. You can specify the repository, description, and files for different platforms.

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
